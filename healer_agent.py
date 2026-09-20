import os
import json
import logging
from fastapi import FastAPI, Request, BackgroundTasks
import git
from openai import OpenAI

app = FastAPI()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

agent_logger = logging.getLogger("autonomous_healer")


def fetch_pipeline_logs(project_id, job_id):
    # In a production GitLab setup, this function can call the GitLab Jobs API.
    agent_logger.info(f"fetching failed execution logs for job {job_id}")

    # Current repository scaffold uses a simulated pipeline failure.
    mock_traceback = (
        "OperationalError: FATAL: the database system is starting up"
    )

    return mock_traceback


# Read the API key from the environment.
api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

if not api_key:
    agent_logger.warning(
        "No LLM API key found. Set LLM_API_KEY or OPENAI_API_KEY before testing."
    )


def generate_infrastructure_patch(logs, repo_context_path):
    """
    Send pipeline logs to the live LLM and request a structured patch.
    """

    agent_logger.info("analyzing trace logs with live LLM")

    if not api_key:
        raise RuntimeError(
            "LLM API key is not configured. "
            "Set LLM_API_KEY or OPENAI_API_KEY."
        )

    client = OpenAI(api_key=api_key)

    prompt = f"""
You are an autonomous DevOps healing agent.

Analyze the following failed CI/CD pipeline log:

{logs}

Repository context:
{repo_context_path}

Determine the configuration/code change required to fix the failure.

Return ONLY valid JSON in exactly this structure:

{{
    "target_file": "relative/path/to/file",
    "updated_content": "complete new contents of the file",
    "explanation": "short explanation of the fix"
}}

Do not use Markdown code fences.
Do not include any text outside the JSON object.
"""

    response = client.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    response_text = response.output_text.strip()

    agent_logger.info("received patch from LLM")

    try:
        patch_payload = json.loads(response_text)
    except json.JSONDecodeError as error:
        agent_logger.error("LLM returned invalid JSON")
        raise ValueError(
            f"LLM response was not valid JSON: {response_text}"
        ) from error

    required_fields = {
        "target_file",
        "updated_content",
        "explanation"
    }

    if not required_fields.issubset(patch_payload):
        raise ValueError(
            "LLM patch response is missing required fields."
        )

    return patch_payload


def deploy_patch_to_repository(repo_path, patch_payload, failure_id):
    """
    Create a branch, apply the LLM-generated patch,
    commit it, and push it to origin.
    """

    agent_logger.info("applying generated patch to a new isolated branch")

    try:
        repo = git.Repo(repo_path)

        branch_name = f"auto-healer-patch-{failure_id}"

        # Remove an existing local branch with the same name if necessary.
        if branch_name in repo.heads:
            repo.delete_head(branch_name, force=True)

        new_branch = repo.create_head(branch_name)
        new_branch.checkout()

        target_file = patch_payload["target_file"]

        # Prevent the LLM from writing outside the repository.
        target_file_path = os.path.abspath(
            os.path.join(repo_path, target_file)
        )

        repo_root = os.path.abspath(repo_path)

        if not target_file_path.startswith(repo_root + os.sep):
            raise ValueError("Invalid target file path returned by LLM.")

        os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

        with open(target_file_path, "w", encoding="utf-8") as file:
            file.write(patch_payload["updated_content"])

        repo.index.add([target_file])

        repo.index.commit(
            f"autonomous self healing patch for pipeline failure {failure_id}"
        )

        # Push the newly created branch to origin.
        origin = repo.remote("origin")
        origin.push(refspec=f"{branch_name}:{branch_name}")

        agent_logger.info(
            f"successfully pushed branch '{branch_name}' to origin"
        )

        return branch_name

    except Exception as error:
        agent_logger.error(
            f"failed to execute git patch operations: {error}"
        )
        return None


def orchestration_loop(webhook_payload):
    """
    Main execution loop for the healing agent.
    """

    agent_logger.info("processing pipeline failure webhook data")

    project_identifier = webhook_payload.get(
        "project", {}
    ).get(
        "id",
        "local_project"
    )

    failed_job_identifier = webhook_payload.get(
        "build_id",
        "test_job_001"
    )

    # The agent operates on the repository containing this script.
    target_repo_path = os.path.dirname(
        os.path.abspath(__file__)
    )

    try:
        crash_logs = fetch_pipeline_logs(
            project_identifier,
            failed_job_identifier
        )

        infrastructure_patch = generate_infrastructure_patch(
            crash_logs,
            target_repo_path
        )

        recovery_branch = deploy_patch_to_repository(
            target_repo_path,
            infrastructure_patch,
            failed_job_identifier
        )

        if recovery_branch:
            agent_logger.info(
                f"healing orchestration complete. "
                f"review branch: {recovery_branch}"
            )
        else:
            agent_logger.error(
                "healing orchestration failed during deployment phase"
            )

    except Exception as error:
        agent_logger.error(
            f"healing orchestration failed: {error}"
        )


@app.post("/webhook")
async def pipeline_failure_webhook(
    request: Request,
    background_tasks: BackgroundTasks
):
    payload = await request.json()

    # Only trigger the healing agent on failed pipeline jobs.
    if payload.get("build_status") == "failed":
        agent_logger.info(
            "detected pipeline failure signature, "
            "initiating self healing routine"
        )

        background_tasks.add_task(
            orchestration_loop,
            payload
        )

        return {
            "status": "healing routine initiated"
        }

    return {
        "status": "ignored, pipeline healthy"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000
    )
