import os
import json
import logging

import requests
from fastapi import FastAPI, Request, BackgroundTasks
import git

app = FastAPI()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
agent_logger = logging.getLogger("autonomous_healer")

# the agent may only rewrite infrastructure files, never application code
PATCHABLE_FILES = ("docker-compose.yml", ".gitlab-ci.yml", "DOCKERFILE", "deploy.sh")

GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
MAX_LOG_CHARS = 8000


def fetch_pipeline_logs(project_id, job_id):
    # pull the real job trace from the gitlab jobs api
    agent_logger.info(f"fetching failed execution logs for job {job_id}")
    if not GITLAB_TOKEN:
        raise RuntimeError("GITLAB_TOKEN is not set; cannot fetch pipeline logs")

    response = requests.get(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/jobs/{job_id}/trace",
        headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
        timeout=30,
    )
    response.raise_for_status()

    # traces run to megabytes and the failure is always at the end, so keep the tail
    return response.text[-MAX_LOG_CHARS:]


def read_repository_context(repo_context_path):
    # show the model the current contents of the files it is allowed to rewrite
    context = {}
    for name in PATCHABLE_FILES:
        path = os.path.join(repo_context_path, name)
        if os.path.isfile(path):
            context[name] = open(path, encoding="utf-8").read()
    return context


def build_patch_prompt(logs, context):
    # assemble the system and user messages sent to the llm
    system_prompt = (
        "You are a CI/CD repair agent. Given a failed pipeline trace and the current "
        "configuration files, return the corrected full contents of exactly one file. "
        "Respond with JSON only, shaped as "
        '{"target_file": "<one of the filenames shown>", "updated_content": "<full file>"}. '
        "Never name a file that was not shown to you."
    )
    rendered = "\n\n".join(f"=== {name} ===\n{body}" for name, body in context.items())
    user_prompt = f"Pipeline trace:\n{logs}\n\nCurrent files:\n{rendered}"
    return system_prompt, user_prompt


def generate_infrastructure_patch(logs, repo_context_path):
    # send the stack trace and repository context to the llm to generate a fix
    agent_logger.info("analyzing trace logs with llm to generate code patch")
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set; cannot generate a patch")

    system_prompt, user_prompt = build_patch_prompt(logs, read_repository_context(repo_context_path))

    response = requests.post(
        LLM_API_URL,
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=120,
    )
    response.raise_for_status()

    payload = json.loads(response.json()["choices"][0]["message"]["content"])
    target = payload.get("target_file")

    # the model chooses the file, so confine that choice to files we meant to expose
    if target not in PATCHABLE_FILES:
        raise ValueError(f"model proposed an unexpected target file: {target!r}")
    if not payload.get("updated_content"):
        raise ValueError("model returned an empty patch")

    agent_logger.info(f"llm proposed a patch for {target}")
    return {"target_file": target, "updated_content": payload["updated_content"]}


def deploy_patch_to_repository(repo_path, patch_payload, failure_id):
    # autonomously branch, commit, and push the llm generated patch
    agent_logger.info("applying generated patch to a new isolated branch")
    
    try:
        repo = git.Repo(repo_path)
        branch_name = f"auto-healer-patch-{failure_id}"
        new_branch = repo.create_head(branch_name)
        new_branch.checkout()
        
        target_file_path = os.path.join(repo_path, patch_payload["target_file"])
        with open(target_file_path, "w") as file:
            file.write(patch_payload["updated_content"])
            
        repo.index.add([patch_payload["target_file"]])
        repo.index.commit(f"autonomous self healing patch for pipeline failure {failure_id}")

        # the branch only reaches review if it is actually pushed
        push_results = repo.remote(name="origin").push(refspec=f"{branch_name}:{branch_name}")
        for result in push_results:
            if result.flags & result.ERROR:
                raise RuntimeError(f"push rejected: {result.summary}")

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
    
    crash_logs = fetch_pipeline_logs(project_identifier, failed_job_identifier)
    infrastructure_patch = generate_infrastructure_patch(crash_logs, target_repo_path)
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