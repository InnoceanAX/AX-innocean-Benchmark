# -*- coding: utf-8 -*-
"""INNOCEAN Benchmark → Cloud Run 배포기 (gcloud 불필요, REST + SA 키).

DB팀 deploy/deploy.py 패턴 재사용. Cloud Run *서비스*(공개) 배포 + Gemini 시크릿.
실행:
  python deploy.py            # 빌드+배포(라이브 innocean-benchmark 갱신)
  python deploy.py --build    # 빌드까지만
  python deploy.py --verify   # 배포 후 라이브 검증만
런타임 SA = perf-data-analyst (BigQuery 마트 접근). 인증 = setup SA 키(ADC).
"""
import os
import sys, sys, io, time, base64, tarfile, argparse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent              # 솔루션/BenchMark
SA_KEY = (ROOT.parent.parent / "setup" / "innocean-perf-apac-kr-40e02bc0d0d8.json")
GEMINI_KEY_FILE = (ROOT.parent.parent / "setup" / "innocean-gemini-api_aistudio.txt")

PROJECT = "innocean-perf-apac-kr"
REGION = "asia-northeast3"
SERVICE = "innocean-benchmark"
# 빌더 SA: 통합뷰 읽기 + 마트 쓰기 (넓은 권한) → 마트 갱신 Job 전용
RUNTIME_SA = f"perf-data-analyst@{PROJECT}.iam.gserviceaccount.com"
# 서비스 SA: 마트 읽기 + 시크릿만 (최소권한) → 공개 서비스 전용
SERVICE_SA = f"benchmark-app@{PROJECT}.iam.gserviceaccount.com"
REPO = "cloud-run-source-deploy"                           # 기존 AR repo 재사용
IMAGE = "innocean-benchmark"
TAG = "backend-v106"
IMG_URI = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPO}/{IMAGE}:{TAG}"
STAGE_BUCKET = "innocean-perf-apac-kr-cloudbuild-source"
SRC_OBJECT = "benchmark/source.tar.gz"
GEMINI_SECRET = "benchmark-gemini-key"

os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(SA_KEY))
import google.auth
from google.auth.transport.requests import AuthorizedSession
creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
S = AuthorizedSession(creds)


def _ok(r, *extra):
    if r.status_code not in (200, 201, *extra):
        raise RuntimeError(f"{r.request.method} {r.url}\n-> {r.status_code} {r.text[:700]}")
    return r


def _wait_op(op_url):
    for _ in range(180):
        d = S.get(op_url).json()
        if d.get("done"):
            if d.get("error"):
                raise RuntimeError(f"op error: {d['error']}")
            return d
        time.sleep(5)
    raise RuntimeError("op timeout: " + op_url)


def ensure_repo():
    url = f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/repositories"
    if S.get(f"{url}/{REPO}").status_code == 200:
        print("· AR repo 존재:", REPO); return
    r = _ok(S.post(f"{url}?repositoryId={REPO}", json={"format": "DOCKER"}))
    _wait_op(f"https://artifactregistry.googleapis.com/v1/{r.json()['name']}")
    print("· AR repo 생성:", REPO)


def ensure_secret():
    base = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets"
    if not GEMINI_KEY_FILE.exists():
        print("· [경고] Gemini 키 파일 없음 — AI는 폴백 모드:", GEMINI_KEY_FILE); return False
    if S.get(f"{base}/{GEMINI_SECRET}").status_code != 200:
        _ok(S.post(f"{base}?secretId={GEMINI_SECRET}", json={"replication": {"automatic": {}}}))
        print("· 시크릿 생성:", GEMINI_SECRET)
    token = GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
    _ok(S.post(f"{base}/{GEMINI_SECRET}:addVersion",
               json={"payload": {"data": base64.b64encode(token.encode()).decode()}}))
    # 런타임 SA 접근권한
    pol = S.get(f"{base}/{GEMINI_SECRET}:getIamPolicy").json()
    binds = pol.get("bindings", [])
    member = f"serviceAccount:{RUNTIME_SA}"
    b = next((x for x in binds if x["role"] == "roles/secretmanager.secretAccessor"), None)
    if not b:
        binds.append({"role": "roles/secretmanager.secretAccessor", "members": [member]})
    elif member not in b["members"]:
        b["members"].append(member)
    _ok(S.post(f"{base}/{GEMINI_SECRET}:setIamPolicy", json={"policy": {"bindings": binds}}))
    print("· 시크릿 버전+권한 갱신:", GEMINI_SECRET); return True


def build_image():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(ROOT / "Dockerfile", arcname="Dockerfile")
        tar.add(ROOT / "backend", arcname="backend",
                filter=lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti)
        tar.add(ROOT / "index.html", arcname="index.html")
    buf.seek(0)
    from google.cloud import storage
    storage.Client(project=PROJECT).bucket(STAGE_BUCKET).blob(SRC_OBJECT).upload_from_file(buf)
    print(f"· 소스 업로드 gs://{STAGE_BUCKET}/{SRC_OBJECT}")
    build = {
        "source": {"storageSource": {"bucket": STAGE_BUCKET, "object": SRC_OBJECT}},
        "steps": [{"name": "gcr.io/cloud-builders/docker",
                   "args": ["build", "--no-cache", "-t", IMG_URI, "-f", "Dockerfile", "."]}],
        "images": [IMG_URI],
    }
    r = _ok(S.post(f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT}/builds", json=build))
    bid = r.json()["metadata"]["build"]["id"]
    print(f"· 빌드 시작 {bid} — 대기...")
    for _ in range(180):
        st = S.get(f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT}/builds/{bid}").json()
        if st.get("status") in ("SUCCESS", "FAILURE", "TIMEOUT", "CANCELLED"):
            print("· 빌드 상태:", st["status"])
            if st["status"] != "SUCCESS":
                raise RuntimeError("빌드 실패 로그: " + st.get("logUrl", ""))
            return
        time.sleep(10)
    raise RuntimeError("빌드 타임아웃")


def deploy_service(with_secret=True):
    base = f"https://{REGION}-run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services"
    env = []   # PORT 는 Cloud Run 이 자동 주입(예약어) → 설정 금지
    if with_secret:
        env.append({"name": "GEMINI_API_KEY",
                    "valueSource": {"secretKeyRef": {"secret": GEMINI_SECRET, "version": "latest"}}})
    body = {
        "ingress": "INGRESS_TRAFFIC_ALL",
        "template": {
            "serviceAccount": SERVICE_SA,   # 최소권한: 마트 읽기 + 시크릿만
            "containers": [{
                "image": IMG_URI,
                "ports": [{"containerPort": 8080}],
                "env": env,
                "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
            }],
            "timeout": "60s",
        },
    }
    exists = S.get(f"{base}/{SERVICE}").status_code == 200
    if exists:
        r = _ok(S.patch(f"{base}/{SERVICE}", json=body))
    else:
        r = _ok(S.post(f"{base}?serviceId={SERVICE}", json=body))
    _wait_op(f"https://{REGION}-run.googleapis.com/v2/{r.json()['name']}")
    # 공개 접근 (allUsers invoker)
    pol = {"policy": {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}}
    _ok(S.post(f"{base}/{SERVICE}:setIamPolicy", json=pol))
    uri = S.get(f"{base}/{SERVICE}").json().get("uri")
    print(f"· Cloud Run {'갱신' if exists else '생성'} 완료: {SERVICE}")
    print("· URL:", uri)
    return uri


MART_JOB = "benchmark-mart-builder"


def deploy_mart_job():
    """마트 일일 갱신용 Cloud Run Job (같은 이미지, command=python mart.py)."""
    base = f"https://{REGION}-run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs"
    body = {"template": {"template": {
        "containers": [{"image": IMG_URI, "command": ["python", "mart.py"],
                        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}],
        "serviceAccount": RUNTIME_SA, "maxRetries": 1, "timeout": "600s",
    }}}
    exists = S.get(f"{base}/{MART_JOB}").status_code == 200
    if exists:
        r = _ok(S.patch(f"{base}/{MART_JOB}", json=body))
    else:
        r = _ok(S.post(f"{base}?jobId={MART_JOB}", json=body))
    _wait_op(f"https://{REGION}-run.googleapis.com/v2/{r.json()['name']}")
    print(f"· Cloud Run Job {'갱신' if exists else '생성'}: {MART_JOB}")


def deploy_scheduler():
    """매일 05:00 KST(수집 03:00 이후) 마트 갱신 트리거."""
    parent = f"projects/{PROJECT}/locations/{REGION}"
    name = f"{parent}/jobs/{MART_JOB}-trigger"
    uri = (f"https://{REGION}-run.googleapis.com/apis/run.googleapis.com/v1/"
           f"namespaces/{PROJECT}/jobs/{MART_JOB}:run")
    body = {"name": name, "schedule": "0 5 * * *", "timeZone": "Asia/Seoul",
            "httpTarget": {"uri": uri, "httpMethod": "POST",
                           "oauthToken": {"serviceAccountEmail": RUNTIME_SA}}}
    g = S.get(f"https://cloudscheduler.googleapis.com/v1/{name}")
    if g.status_code == 200:
        _ok(S.patch(f"https://cloudscheduler.googleapis.com/v1/{name}"
                    f"?updateMask=schedule,timeZone,httpTarget", json=body))
        print("· Scheduler 갱신: 매일 05:00 KST")
    else:
        _ok(S.post(f"https://cloudscheduler.googleapis.com/v1/{parent}/jobs", json=body))
        print("· Scheduler 생성: 매일 05:00 KST")


def run_mart_job_now():
    base = f"https://{REGION}-run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs"
    r = _ok(S.post(f"{base}/{MART_JOB}:run"))
    print("· 마트 Job 수동 1회 실행 트리거:", r.json().get("name", "")[:80])


def verify():
    base = f"https://{REGION}-run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services"
    uri = S.get(f"{base}/{SERVICE}").json().get("uri")
    import urllib.request
    def hit(path):
        try:
            with urllib.request.urlopen(uri + path, timeout=30) as r:
                return r.status, r.read(200).decode("utf-8", "ignore")
        except Exception as e:
            return "ERR", str(e)[:120]
    print("\n=== 라이브 검증:", uri, "===")
    print(" /healthz:", hit("/api/v1/healthz"))
    print(" /:", hit("/")[0], "(index)")
    print(" /api/v1/benchmark?media=M:", hit("/api/v1/benchmark?media=M&date_from=2025-06-01&date_to=2026-06-08")[0])
    return uri


def wait_job_exec(exec_name):
    """마트 Job 실행(execution) 완료 대기."""
    url = f"https://{REGION}-run.googleapis.com/v2/{exec_name}"
    for _ in range(120):
        d = S.get(url).json()
        cond = {c.get("type"): c.get("state") for c in (d.get("conditions") or [])}
        if d.get("completionTime") or cond.get("Completed") in ("CONDITION_SUCCEEDED", "CONDITION_FAILED"):
            ok = cond.get("Completed") == "CONDITION_SUCCEEDED" or (d.get("succeededCount", 0) >= 1)
            print("· 마트 Job 실행", "성공" if ok else f"종료(상태 미확정: {cond})")
            return ok
        time.sleep(5)
    print("· 마트 Job 실행 폴링 타임아웃(백그라운드 계속될 수 있음)")
    return False


def run_mart_job_now_wait():
    base = f"https://{REGION}-run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs"
    r = _ok(S.post(f"{base}/{MART_JOB}:run"))
    exec_name = r.json().get("metadata", {}).get("name") or r.json().get("name", "")
    print("· 마트 Job 수동 실행:", exec_name[:90])
    if exec_name:
        return wait_job_exec(exec_name)
    return False


def check_tests():
    """배포 전 단위 테스트 — 지금은 캐시 곡선 해석기.

    ★ 왜 게이트에 두나
      벤더 곡선은 자격증명이 풀릴 때까지 한 행도 안 들어온다. 그 사이 해석기를 고치면
      «돌려 볼 데이터가 없어» 검증 없이 배포된다. 가짜 행으로 대는 테스트를 게이트에
      두면 그 구멍이 막힌다 — 프론트 문법 검사를 넣은 것과 같은 이유다.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    for t in ("test_reach_cache.py",):
        f = os.path.join(here, t)
        if not os.path.exists(f):
            continue
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, f], cwd=here, env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stdout or "")[-1200:] + (r.stderr or "")[-400:]
            print(f"· [테스트 실패] {t}")
            print(tail)
            return False
        print(f"· 테스트 통과 {t}")
    return True


def check_frontend():
    """index.html 의 모든 <script> 블록을 node --check 로 파싱한다.

    ★ 왜 — 2026-09-22 에 툴팁 문자열 안에 실제 줄바꿈이 들어가 JS 문법 오류가 났고,
      **그 블록 전체(기간비교·분포폭·업종배지·경고배너)가 통째로 죽은 채 배포됐다.**
      화면은 나머지 블록이 그려서 «대충 도는 것처럼» 보였고, 콘솔 에러 하나만 남았다.
      오늘 하루 배운 것 그대로다 — 조용한 실패는 사람이 아니라 기계가 잡아야 한다.
    node 가 없으면 건너뛴다(경고만). 있으면 오류 시 배포를 멈춘다.
    """
    import re
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    idx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")
    if not node:
        print("· [경고] node 없음 → 프론트 문법 검사 생략")
        return True
    src = io.open(idx, encoding="utf-8").read()
    bad = []
    for i, m in enumerate(re.finditer(r"<script>", src), 1):
        end = src.find("</script>", m.start())
        js = src[m.start() + 8:end]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                line = src[:m.start()].count(chr(10)) + 1
                bad.append((i, line, (r.stderr or "")[:400]))
        finally:
            os.unlink(tmp)
    if bad:
        print(f"🔴 프론트 문법 오류 {len(bad)}건 — 배포를 중단합니다")
        for i, line, err in bad:
            print(f"   블록{i} (index.html line {line}):"); print(err)
        return False
    print("· 프론트 문법 검사 통과 (script 블록 전부)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="배포 후 마트를 즉시 재빌드(야간 스케줄을 기다리지 않음)")
    ap.add_argument("--service-only", action="store_true",
                    help="마트 Job 이미지 갱신을 건너뛴다. 야간 빌더가 구 코드로 남으므로 "
                         "mart.py 를 건드리지 않은 경우에만 쓸 것")
    a = ap.parse_args()
    print(f"배포 대상: {PROJECT}/{REGION}/{SERVICE}  (deployer={creds.service_account_email})")
    if a.verify:
        verify(); return
    if not check_frontend():
        raise SystemExit("프론트 문법 오류로 배포를 중단했습니다")
    if not check_tests():
        raise SystemExit("단위 테스트 실패로 배포를 중단했습니다")
    ensure_repo()
    has_secret = ensure_secret()
    build_image()
    if a.build:
        print("빌드 완료 (배포 생략)"); return
    deploy_service(with_secret=has_secret)

    # 🔴 마트 Job 이미지 갱신은 «기본» 이다 (2026-10-21 사고로 바꿨다)
    #   전에는 --full 에서만 돌렸다. 그 결과 서비스는 새 코드, 야간 마트 빌더 Job 은
    #   구버전 이미지로 남았고, 스케줄이 구 mart.py 로 마트를 «덮어» 새 컬럼이 사라졌다.
    #   실제 피해: 2026-10-20 야간 빌드가 delivery_days_actual 을 떨어뜨려
    #   /api/v1/reach/* 가 전부 500 이 됐다. 서비스는 200 이라 배포 검증도 통과했다.
    #   ⚠️ 같은 사고가 전에 channel 컬럼으로도 있었다(아래 run_mart_job_now_wait 주석).
    #     플래그로 막는 방식이 두 번 실패했으므로 기본 동작으로 올린다.
    #   이미지 갱신은 멱등하고 몇 초다. 안 하는 쪽의 비용이 «조용한 데이터 파손» 이다.
    if not a.service_only:
        deploy_mart_job()
    if a.full:
        run_mart_job_now_wait()  # 마트 즉시 재빌드(야간을 기다리지 않을 때)
    verify()
    print("\n✅ 배포 완료.")


if __name__ == "__main__":
    main()
