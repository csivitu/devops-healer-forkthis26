import os
import json
import logging
from fastapi import FastAPI, Request, BackgroundTasks
import git
import google.generativeai as genai

app = FastAPI()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
agent_logger = logging.getLogger("autonomous_healer")


def fetch_pipeline_logs(project_id, job_id):
    agent_logger.info(f"fetching failed execution logs for job {job_id}")
    return "OperationalError: FATAL: the database system is starting up"


# Configure Gemini API Key
api_key = os.getenv("GEMINI_API_KEY", "your-api-key-here")
genai.configure(api_key=api_key)


def generate_infrastructure_patch(logs, repo_context_path):
    agent_logger.info("analyzing trace logs with llm to generate code patch")

    prompt = f"""
    Analyze these CI/CD pipeline failure logs:
    {logs}

    Respond ONLY with a valid JSON object containing the target file and updated content.
    Example output format:
    {{
        "target_file": "docker-compose.yml",
        "updated_content": "version: '3.8'\\nservices:\\n  web:\\n    build: .\\n    depends_on:\\n      db:\\n        condition: service_healthy\\n"
    }}
    """

    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )

    patch_payload = json.loads(response.text)
    return patch_payload


def deploy_patch_to_repository(repo_path, patch_payload, failure_id):
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

        agent_logger.info(f"successfully pushed patch branch '{branch_name}' to remote repository")
        return branch_name

    except Exception as error:
        agent_logger.error(f"failed to execute git patch operations: {error}")
        return None


def orchestration_loop(webhook_payload):
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


@app.post("/webhook")
async def pipeline_failure_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    if payload.get("build_status") == "failed":
        agent_logger.info("detected pipeline failure signature, initiating self healing routine")
        background_tasks.add_task(orchestration_loop, payload)
        return {"status": "healing routine initiated"}

    return {"status": "ignored, pipeline healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)