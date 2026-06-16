# -*- coding: utf-8 -*-
"""INNOCEAN Benchmark → Cloud Run 배포기 (gcloud 불필요, REST + SA 키).

DB팀 deploy/deploy.py 패턴 재사용. Cloud Run *서비스*(공개) 배포 + Gemini 시크릿.
실행:
  python deploy.py            # 빌드+배포(라이브 innocean-benchmark 갱신)
  python deploy.py --build    # 빌드까지만
  python deploy.py --verify   # 배포 후 라이브 검증만
런타임 SA = perf-data-analyst (BigQuery 마트 접근). 인증 = setup SA 키(ADC).
"""
import os, sys, io, time, base64, tarfile, argparse
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
TAG = "backend-v24"
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
    print(" /healthz:", hit("/healthz"))
    print(" /:", hit("/")[0], "(index)")
    print(" /api/v1/benchmark?media=M:", hit("/api/v1/benchmark?media=M&date_from=2025-06-01&date_to=2026-06-08")[0])
    return uri


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    print(f"배포 대상: {PROJECT}/{REGION}/{SERVICE}  (deployer={creds.service_account_email})")
    if a.verify:
        verify(); return
    ensure_repo()
    has_secret = ensure_secret()
    build_image()
    if a.build:
        print("빌드 완료 (배포 생략)"); return
    deploy_service(with_secret=has_secret)
    verify()
    print("\n✅ 배포 완료.")


if __name__ == "__main__":
    main()
