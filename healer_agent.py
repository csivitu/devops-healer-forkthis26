import os
import json
import logging
import google.generativeai as genai
from fastapi import FastAPI, Request, BackgroundTasks
import git

app = FastAPI()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
agent_logger = logging.getLogger("autonomous_healer")

def fetch_pipeline_logs(project_id, job_id):
    # simulate fetching the raw compilation or execution logs from the gitlab api
    agent_logger.info(f"fetching failed execution logs for job {job_id}")
    mock_traceback = "OperationalError: FATAL: the database system is starting up"
    return mock_traceback

# configure your llm client or api key right here before testing
api_key = os.getenv("LLM_API_KEY", "your-api-key-here")

def generate_infrastructure_patch(logs, repo_context_path):
    # send the stack trace and repository context to the llm to generate a fix
    agent_logger.info("analyzing trace logs with llm to generate code patch")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    llm_prompt = (
        f"You are a DevOps expert. Analyze these CI/CD pipeline error logs and return ONLY valid JSON "
        f"with two keys: 'target_file' (string, the file to fix) and 'updated_content' (string, the corrected file content).\n\n"
        f"Error logs:\n{logs}\n\n"
        f"Respond with JSON only, no markdown fences."
    )

    try:
        response = model.generate_content(llm_prompt)
        patch_payload = json.loads(response.text.strip())
        agent_logger.info(f"llm returned patch targeting: {patch_payload.get('target_file')}")
        return patch_payload
    except Exception as e:
        agent_logger.error(f"llm call failed: {e}, falling back to default patch")
        return {
            "target_file": "sandbox/docker-compose.yml",
            "updated_content": "version: '3.8'\nservices:\n  web:\n    build: .\n    depends_on:\n      db:\n        condition: service_healthy\n"
        }

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

        origin = repo.remote(name="origin")
        origin.push(refspec=f"{branch_name}:{branch_name}")
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