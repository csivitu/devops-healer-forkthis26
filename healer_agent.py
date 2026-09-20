import os
import json
import logging
import requests
from fastapi import FastAPI, Request, BackgroundTasks
import git

app = FastAPI()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
agent_logger = logging.getLogger("autonomous_healer")

# configure your llm client or api key right here before testing
api_key = os.getenv("LLM_API_KEY", "your-api-key-here")
# any openai compatible chat completions endpoint works here (openai, gemini, groq, ollama, ...)
api_base = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")
llm_model = os.getenv("LLM_MODEL", "gpt-4o-mini")

# files the model is most likely to need when repairing the pipeline
CONTEXT_FILES = ["docker-compose.yml", ".gitlab-ci.yml", "deploy.sh", "DOCKERFILE", "app.py"]

def fetch_pipeline_logs(project_id, job_id):
    # pull the raw job trace from the gitlab api when we have a token, otherwise fall back to the canned trace
    agent_logger.info(f"fetching failed execution logs for job {job_id}")
    gitlab_token = os.getenv("GITLAB_TOKEN")
    if gitlab_token:
        gitlab_api = os.getenv("CI_API_V4_URL", "https://gitlab.com/api/v4")
        response = requests.get(
            f"{gitlab_api}/projects/{project_id}/jobs/{job_id}/trace",
            headers={"PRIVATE-TOKEN": gitlab_token},
            timeout=30,
        )
        response.raise_for_status()
        return response.text
    mock_traceback = "OperationalError: FATAL: the database system is starting up"
    return mock_traceback

def collect_repository_context(repo_context_path):
    # hand the model the current contents of the files it may need to patch
    context = {}
    for name in CONTEXT_FILES:
        path = os.path.join(repo_context_path, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as file:
                context[name] = file.read()[:6000]
    return context

def generate_infrastructure_patch(logs, repo_context_path):
    # send the stack trace and repository context to the llm to generate a fix
    agent_logger.info("analyzing trace logs with llm to generate code patch")
    if api_key == "your-api-key-here":
        raise RuntimeError("LLM_API_KEY is not set, the agent cannot generate a patch")

    repo_context = collect_repository_context(repo_context_path)
    llm_prompt = (
        "you are a devops engineer repairing a broken ci cd pipeline.\n"
        "below are the failed pipeline logs and the current contents of the repository files.\n"
        "pick the single file that needs to change and return its complete corrected contents.\n"
        "respond with json only, no prose and no markdown fences, in exactly this shape:\n"
        '{"target_file": "<path relative to the repository root>", "updated_content": "<full new file contents>"}\n\n'
        f"pipeline logs:\n{logs}\n\n"
        + "".join(f"--- {name} ---\n{content}\n\n" for name, content in repo_context.items())
    )

    response = requests.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": llm_model,
            "temperature": 0,
            "messages": [{"role": "user", "content": llm_prompt}],
        },
        timeout=120,
    )
    response.raise_for_status()
    answer = response.json()["choices"][0]["message"]["content"].strip()
    if answer.startswith("```"):
        # strip a markdown fence if the model added one anyway
        answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]

    patch_payload = json.loads(answer)
    if not {"target_file", "updated_content"} <= patch_payload.keys():
        raise ValueError(f"llm returned an unexpected payload: {patch_payload}")
    agent_logger.info(f"llm proposed a patch for {patch_payload['target_file']}")
    return patch_payload

def deploy_patch_to_repository(repo_path, patch_payload, failure_id):
    # autonomously branch, commit, and push the llm generated patch
    agent_logger.info("applying generated patch to a new isolated branch")

    try:
        # the sandbox lives inside the main repository, so walk up to find the .git directory
        repo = git.Repo(repo_path, search_parent_directories=True)
        repo_root = os.path.normcase(os.path.abspath(repo.working_tree_dir))
        target_file_path = os.path.normcase(os.path.abspath(os.path.join(repo_path, patch_payload["target_file"])))
        if os.path.commonpath([repo_root, target_file_path]) != repo_root:
            raise ValueError(f"refusing to write outside the repository: {patch_payload['target_file']}")

        branch_name = f"auto-healer-patch-{failure_id}"
        new_branch = repo.create_head(branch_name)
        new_branch.checkout()

        with open(target_file_path, "w", encoding="utf-8") as file:
            file.write(patch_payload["updated_content"])

        repo.index.add([os.path.relpath(target_file_path, repo_root)])
        repo.index.commit(f"autonomous self healing patch for pipeline failure {failure_id}")
        repo.git.push("--set-upstream", "origin", branch_name)

        agent_logger.info(f"successfully pushed patch branch '{branch_name}' to remote repository")
        return branch_name

    except Exception as error:
        agent_logger.error(f"failed to execute git patch operations: {error}")
        return None

def orchestration_loop(webhook_payload):
    # main execution loop for the healing agent
    agent_logger.info("processing pipeline failure webhook data")
    project_identifier = webhook_payload.get("project", {}).get("id", "local_project")
    failed_job_identifier = webhook_payload.get("build_id", "test_job_001")
    target_repo_path = "./sandbox"

    try:
        crash_logs = fetch_pipeline_logs(project_identifier, failed_job_identifier)
        infrastructure_patch = generate_infrastructure_patch(crash_logs, target_repo_path)
    except Exception as error:
        agent_logger.error(f"healing orchestration failed before a patch could be produced: {error}")
        return

    recovery_branch = deploy_patch_to_repository(target_repo_path, infrastructure_patch, failed_job_identifier)

    if recovery_branch:
        agent_logger.info(f"healing orchestration complete. review branch: {recovery_branch}")
    else:
        agent_logger.error("healing orchestration failed during deployment phase")

# hit this endpoint with postman to test the loop. address is post http://localhost:8000/webhook
# raw json body needs to be exactly {"build_status": "failed", "build_id": "test_job_001"}
@app.post("/webhook")
async def pipeline_failure_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    # only trigger the healing agent on failed pipeline jobs
    if payload.get("build_status") == "failed":
        agent_logger.info("detected pipeline failure signature, initiating self healing routine")
        background_tasks.add_task(orchestration_loop, payload)
        return {"status": "healing routine initiated"}

    return {"status": "ignored, pipeline healthy"}

# run this server locally so postman can reach it on port 8000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
