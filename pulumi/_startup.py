"""Cloud-agnostic Ubuntu startup script building blocks shared by _azure.py and _aws.py."""


def install_packages_and_docker() -> str:
    return r"""apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg-agent software-properties-common git jq unzip python3 apache2-utils

# Docker + Compose
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -
add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable"
apt-get update -y && apt-get install -y docker-ce docker-ce-cli containerd.io
curl -L "https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose"""


def clone_repo(repo_url: str, branch: str) -> str:
    return f"""REPO_URL="{repo_url}"
REPO_PATH=$(echo "$REPO_URL" | sed 's,https://,,')
git clone "https://$GIT_USER:$GIT_TOKEN@$REPO_PATH" /home/ubuntu/trino
git -C /home/ubuntu/trino checkout {branch}
git -C /home/ubuntu/trino rev-parse HEAD > /home/ubuntu/trino/.schema-version"""


def write_env_from_secret_json() -> str:
    return r"""echo "$SECRET_JSON" | jq -r 'to_entries[] | .key + "=" + (.value | tostring)' > /home/ubuntu/trino/.env
chmod 600 /home/ubuntu/trino/.env"""


def generate_config_and_start(worker_count: int) -> str:
    return f"""cd /home/ubuntu/trino
python3 config_generator.py envs_prod.json --env-file .env
docker-compose -f docker-compose.yml up -d --scale trino-worker={worker_count}"""
