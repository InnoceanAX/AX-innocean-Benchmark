# -*- coding: utf-8 -*-
"""ref/netflix_Credentials.txt 를 읽어 Secret Manager 에 넣는다 (개발·운영 한 번에).

왜 이 파일이 있나
  DB 의 deploy/set_netflix_key.py 는 getpass 로 «사람이 붙여넣는» 도구다. 값이 이미
  파일로 와 있으면 두 번 복사·붙여넣기를 해야 하고, 그 과정에서 공백·줄바꿈이 섞여
  길이가 틀어지는 일이 잦다. 이 스크립트는 파일에서 직접 읽어 그 틈을 없앤다.

  🔴 그래도 «평문 파일을 읽는다» 는 약점이 있다 (DB 지적, 2026-10-22)
    set_netflix_key.py 가 getpass 인 것은 «설계대로» 다 — 값이 명령행 히스토리·로그·
    파일에 남지 않게 사람 터미널을 전제로 만든 것이다. 이 스크립트는 그 전제를 깬다.
    파일이 «이미» 디스크에 있어서(사용자가 둔 것) 새 노출을 만들지는 않지만,
    주입이 끝나면 파일은 남을 이유가 없다 — 끝에 지우는 법을 안내하고,
    --shred 로 덮어쓴 뒤 삭제할 수 있게 한다.

  ⚠️ 값은 화면에 찍지 않는다. 길이만 확인한다.
  ⚠️ 멱등하다 — 다시 돌리면 새 «버전» 이 추가될 뿐 기존 값이 깨지지 않는다.
  ⚠️ ref/ 는 .gitignore 에 있어 자격증명 파일은 커밋되지 않는다.

하는 일
  1. 파일에서 Staging · Production 값을 뽑는다
  2. 시크릿이 없으면 만들고, 새 버전을 추가한다
     netflix-netkey-staging · netflix-netkey-production
  3. 수집 서비스 계정에 읽기 권한을 붙인다

실행: python inject_netflix_key.py
"""
import base64
import io
import os
import re
import sys

PROJECT = "innocean-perf-apac-kr"
SA = "perf-data-analyst@innocean-perf-apac-kr.iam.gserviceaccount.com"
HERE = os.path.dirname(os.path.abspath(__file__))
CRED = os.path.join(HERE, "..", "ref", "netflix_Credentials.txt")
ENVS = [("staging", "netflix-netkey-staging"), ("production", "netflix-netkey-production")]


def _creds_path():
    for k in (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
              os.path.abspath(os.path.join(HERE, "..", "..", "..", "setup",
                                           "innocean-perf-apac-kr-40e02bc0d0d8.json"))):
        if k and os.path.exists(k):
            return k
    return None


def parse(path):
    """파일 → {env: value}. 라벨 줄로 구간을 나누고 긴 토큰을 하나씩 집는다."""
    if not os.path.exists(path):
        sys.exit(f"자격증명 파일이 없습니다: {os.path.abspath(path)}")
    out, cur = {}, None
    for line in io.open(path, encoding="utf-8", errors="replace").read().splitlines():
        s = line.strip()
        if not s:
            continue
        if "Staging" in s:
            cur = "staging"
            continue
        if "Production" in s:
            cur = "production"
            continue
        m = re.search(r"[A-Za-z0-9_\-\.=]{40,}", s)
        if m and cur and cur not in out:
            out[cur] = m.group(0)
    return out


def main():
    key = _creds_path()
    if not key:
        sys.exit("GCP 서비스 계정 키를 찾지 못했습니다 (setup/ 아래 .json)")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key

    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    vals = parse(CRED)
    print("파일에서 읽음:", {k: f"{len(v)}자" for k, v in sorted(vals.items())} or "없음")
    missing = [e for e, _ in ENVS if e not in vals]
    if missing:
        print(f"  주의: {', '.join(missing)} 값을 못 찾았습니다 — 그 환경은 건너뜁니다")

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    S = AuthorizedSession(creds)
    SM = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}"

    def ok(r, *extra):
        if r.status_code not in (200, 201, *extra):
            raise RuntimeError(f"{r.status_code} {r.text[:160]}")
        return r

    done = 0
    for env, sid in ENVS:
        v = vals.get(env)
        if not v:
            continue
        if v.lower().startswith("bearer"):
            print(f"  [X] {env}: 'Bearer ' 접두가 붙어 있습니다 — 키 값만 있어야 합니다")
            continue
        # 🔴 이미 같은 값이 들어 있으면 버전을 더 만들지 않는다 (멱등)
        try:
            cur = S.get(f"{SM}/secrets/{sid}/versions/latest:access")
            if cur.status_code == 200:
                import base64 as _b
                if _b.b64decode(cur.json()["payload"]["data"]).decode() == v:
                    print(f"  [=] {sid}  이미 같은 값 — 건너뜀")
                    done += 1
                    continue
        except Exception:
            pass
        # ⚠️ 한 환경이 실패해도 다른 환경은 계속한다.
        #   전에 staging 의 권한 단계에서 죽어 production 이 «아예 만들어지지 않았다»(실측).
        #   부분 실패를 전체 실패로 만들면, 고친 뒤 처음부터 다시 해야 한다.
        try:
            if S.get(f"{SM}/secrets/{sid}").status_code == 404:
                ok(S.post(f"{SM}/secrets?secretId={sid}",
                          json={"replication": {"automatic": {}}}))
                print(f"  · {sid} 생성")
            ver = ok(S.post(f"{SM}/secrets/{sid}:addVersion",
                            json={"payload": {"data": base64.b64encode(v.encode()).decode()}})).json()
            print(f"  [O] {sid}  버전 {ver['name'].split('/')[-1]} · 길이 {len(v)}자")
        except Exception as e:
            print(f"  [X] {sid} 주입 실패: {str(e)[:120]}")
            continue

        # ⚠️ IAM 부여가 실패해도 «멈추지 않는다». 값은 이미 들어갔고, 그게 핵심이다.
        #   전에 여기서 SystemExit 로 죽어 production 이 아예 안 만들어졌다(실측).
        #   그리고 perf-data-analyst 는 프로젝트 레벨 권한으로 이미 읽을 수 있다 —
        #   명시 바인딩은 «관례» 이지 동작 조건이 아니다. 실패하면 알리고 넘어간다.
        try:
            pol = ok(S.post(f"{SM}/secrets/{sid}:getIamPolicy")).json()
            binds = pol.get("bindings", [])
            mem = f"serviceAccount:{SA}"
            acc = next((b for b in binds
                        if b["role"] == "roles/secretmanager.secretAccessor"), None)
            if acc is None:
                binds.append({"role": "roles/secretmanager.secretAccessor", "members": [mem]})
            elif mem not in acc.get("members", []):
                acc.setdefault("members", []).append(mem)
            else:
                binds = None
            if binds is not None:
                ok(S.post(f"{SM}/secrets/{sid}:setIamPolicy",
                          json={"policy": {"bindings": binds}}))
                print("       읽기 권한 부여 완료")
            else:
                print("       읽기 권한 이미 있음")
        except Exception as e:
            print(f"       (권한 부여 실패 — 값은 들어갔습니다: {str(e)[:80]})")
            print("        perf-data-analyst 는 프로젝트 레벨 권한으로 읽을 수 있어 "
                  "동작에는 지장이 없습니다. 관례대로 명시 바인딩이 필요하면 DB 에 요청하십시오.")
        done += 1

    del vals
    # 주입이 끝났으면 평문 파일은 남을 이유가 없다
    if done and "--shred" in sys.argv:
        try:
            n = os.path.getsize(CRED)
            with open(CRED, "r+b") as f:        # 덮어쓴 뒤 삭제 — 복구를 어렵게 한다
                f.write(b"0" * n)
                f.flush()
                os.fsync(f.fileno())
            os.remove(CRED)
            print(f"  [O] 평문 파일 덮어쓰기 후 삭제: {os.path.basename(CRED)}")
        except Exception as e:
            print(f"  (파일 삭제 실패 — 직접 지우십시오: {str(e)[:70]})")
    print()
    if done:
        print(f"완료 — {done}개 환경 주입했습니다.")
        print("다음: python status.py  (넷플릭스 줄의 자격증명이 [O] 로 바뀌는지 확인)")
        print("그리고 DB 쪽에서 deploy.py 의 SECRET_ENV 매핑 추가 + 재배포가 남습니다")
        if "--shred" not in sys.argv:
            print()
            print("🔴 평문 자격증명 파일이 아직 남아 있습니다:")
            print(f"   {os.path.abspath(CRED)}")
            print("   시크릿에 들어갔으니 지우십시오 — 다시 돌릴 때는 --shred 를 붙이면")
            print("   덮어쓴 뒤 삭제합니다. (원본은 1Password·메일에 있습니다)")
    else:
        print("주입된 것이 없습니다 — 파일 형식을 확인하십시오.")


if __name__ == "__main__":
    main()
