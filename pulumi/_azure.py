"""
DWE Trino Infrastructure — Azure Pulumi IaC
Provisions: Application Gateway (HTTP→HTTPS) + VMSS + Azure DNS

Single-instance VMSS (stateful — Trino needs persistent storage).
Rolling reimage via VMSS reimage: new instance boots, old terminates.
Run with: pulumi stack select prod && pulumi up --yes
"""

import base64
import json
from pathlib import Path

import pulumi
import pulumi_azure_native as azure_native
import yaml
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# ─────────────────────────────────────────────────────────────────────────────
# Hydration config — written by dwe-core at create-service / update-service time
# ─────────────────────────────────────────────────────────────────────────────
_dwe = yaml.safe_load((Path(__file__).parent / "dwe-hydration.yaml").read_text())
project_name    = _dwe["project_name"]
git_repo_url    = _dwe["git_repo_url"]
adapter_version = _dwe["adapter_version"]

# ─────────────────────────────────────────────────────────────────────────────
# Stack Config
# ─────────────────────────────────────────────────────────────────────────────
config = pulumi.Config()
env               = config.require("environment")
git_branch        = config.require("git_branch")
secret_id         = config.require("secret_id")
key_vault_name    = config.require("key_vault_name")
azure_location    = config.get("azure_location") or "eastus"
vm_size           = config.get("instance_type") or "Standard_D4s_v3"
volume_size       = int(config.get("volume_size") or "100")
worker_count      = int(config.get("worker_count") or "1")
resource_group    = config.require("resource_group")
subscription_id   = config.require("subscription_id")
app_port          = 8080
startup_code_version = config.get("startup_code_version") or ""

suffix = f"-{env}" if env != "prod" else ""
tags = {
    "Project":     project_name,
    "ManagedBy":   "Pulumi",
    "Environment": env,
    "GitBranch":   git_branch,
}

# ─────────────────────────────────────────────────────────────────────────────
# Load secrets from Azure Key Vault
# ─────────────────────────────────────────────────────────────────────────────
def get_secret(kv_name: str, sid: str) -> dict:
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=f"https://{kv_name}.vault.azure.net/", credential=credential)
    secret = client.get_secret(sid)
    return json.loads(secret.value)

secrets = get_secret(key_vault_name, secret_id)

# ── Infrastructure ────────────────────────────────────────────────────────────
vnet_id            = secrets["VNET_ID"]
app_gw_subnet_id   = secrets["APP_GW_SUBNET_ID"]
vm_subnet_id       = secrets["VM_SUBNET_ID"]
ssh_public_key     = secrets["SSH_PUBLIC_KEY"]

# ── Networking / DNS ──────────────────────────────────────────────────────────
dns_zone_name      = secrets["DNS_ZONE_NAME"]
dns_record_name    = secrets["DNS_RECORD_NAME"]
dns_zone_rg        = secrets.get("DNS_ZONE_RESOURCE_GROUP", resource_group)
ssl_cert_kv_id     = secrets.get("APP_GW_SSL_CERT_KEY_VAULT_ID", "")

# ── Git ───────────────────────────────────────────────────────────────────────
git_deploy_token    = secrets["git_deploy_token"]
git_deploy_username = secrets.get("git_deploy_username", "x-token-auth")

# ── Trino runtime ─────────────────────────────────────────────────────────────
# Individual values are not used in Python — config_generator.py reads them
# from .env on the VM. Validate presence here to catch missing secrets early.
for _key in ("CATALOG_URL", "ICEBERG_WAREHOUSE_DIR", "TRINO_USER", "TRINO_PASSWORD", "TRINO_SHARED_SECRET"):
    if not secrets.get(_key):
        raise ValueError(f"Required secret '{_key}' missing from Key Vault secret {secret_id}")

# ─────────────────────────────────────────────────────────────────────────────
# User-assigned Managed Identity (used by VMSS to read Key Vault)
# ─────────────────────────────────────────────────────────────────────────────
identity = azure_native.managedidentity.UserAssignedIdentity(
    f"{project_name}-identity{suffix}",
    resource_group_name=resource_group,
    location=azure_location,
    resource_name_=f"{project_name}-identity{suffix}",
    tags=tags,
)

# Key Vault Secrets User role assignment for the Managed Identity (RBAC-enabled vault)
# Role: Key Vault Secrets User (4633458b-17de-408a-b874-0445c86b69e6)
kv_access = azure_native.authorization.RoleAssignment(
    f"{project_name}-kv-role{suffix}",
    scope=pulumi.Output.format(
        "/subscriptions/{0}/resourceGroups/{1}/providers/Microsoft.KeyVault/vaults/{2}",
        subscription_id, resource_group, key_vault_name,
    ),
    role_definition_id=pulumi.Output.format(
        "/subscriptions/{0}/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6",
        subscription_id,
    ),
    principal_id=identity.principal_id,
    principal_type="ServicePrincipal",
)

# ─────────────────────────────────────────────────────────────────────────────
# Public IP for Application Gateway
# ─────────────────────────────────────────────────────────────────────────────
public_ip = azure_native.network.PublicIPAddress(
    f"{project_name}-pip{suffix}",
    resource_group_name=resource_group,
    location=azure_location,
    public_ip_address_name=f"{project_name}-pip{suffix}",
    sku=azure_native.network.PublicIPAddressSkuArgs(name="Standard"),
    public_ip_allocation_method="Static",
    tags=tags,
)

# ─────────────────────────────────────────────────────────────────────────────
# Network Security Groups
# ─────────────────────────────────────────────────────────────────────────────
app_gw_nsg = azure_native.network.NetworkSecurityGroup(
    f"{project_name}-appgw-nsg{suffix}",
    resource_group_name=resource_group,
    location=azure_location,
    network_security_group_name=f"{project_name}-appgw-nsg{suffix}",
    security_rules=[
        azure_native.network.SecurityRuleArgs(
            name="AllowHTTP",
            priority=100, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range="80",
            source_address_prefix="*", destination_address_prefix="*",
        ),
        azure_native.network.SecurityRuleArgs(
            name="AllowHTTPS",
            priority=110, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range="443",
            source_address_prefix="*", destination_address_prefix="*",
        ),
        azure_native.network.SecurityRuleArgs(
            name="AllowGatewayManager",
            priority=120, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range="65200-65535",
            source_address_prefix="GatewayManager", destination_address_prefix="*",
        ),
    ],
    tags=tags,
)

vm_nsg = azure_native.network.NetworkSecurityGroup(
    f"{project_name}-vm-nsg{suffix}",
    resource_group_name=resource_group,
    location=azure_location,
    network_security_group_name=f"{project_name}-vm-nsg{suffix}",
    security_rules=[
        azure_native.network.SecurityRuleArgs(
            name="AllowAppPort",
            priority=100, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range=str(app_port),
            source_address_prefix="VirtualNetwork", destination_address_prefix="*",
        ),
        azure_native.network.SecurityRuleArgs(
            name="AllowSSH",
            priority=110, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range="22",
            source_address_prefix="VirtualNetwork", destination_address_prefix="*",
        ),
    ],
    tags=tags,
)

# ─────────────────────────────────────────────────────────────────────────────
# Application Gateway (HTTP→HTTPS redirect + HTTPS→VMSS backend)
# Internal sub-resource IDs are constructed from known config values.
# ─────────────────────────────────────────────────────────────────────────────
app_gw_name = f"{project_name}-appgw{suffix}"
ag_prefix = (
    f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    f"/providers/Microsoft.Network/applicationGateways/{app_gw_name}"
)

ssl_certs = []
if ssl_cert_kv_id:
    ssl_certs = [azure_native.network.ApplicationGatewaySslCertificateArgs(
        name="tls-cert",
        key_vault_secret_id=ssl_cert_kv_id,
    )]

has_ssl = bool(ssl_certs)

http_listeners = [
    azure_native.network.ApplicationGatewayHttpListenerArgs(
        name="http-listener",
        frontend_ip_configuration=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/frontendIPConfigurations/appGwPublicFrontendIp"),
        frontend_port=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/frontendPorts/port_80"),
        protocol="Http",
    ),
]

redirect_configurations = []
if has_ssl:
    http_listeners.append(azure_native.network.ApplicationGatewayHttpListenerArgs(
        name="https-listener",
        frontend_ip_configuration=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/frontendIPConfigurations/appGwPublicFrontendIp"),
        frontend_port=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/frontendPorts/port_443"),
        protocol="Https",
        ssl_certificate=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/sslCertificates/tls-cert"),
    ))
    redirect_configurations = [azure_native.network.ApplicationGatewayRedirectConfigurationArgs(
        name="redirect-to-https",
        redirect_type="Permanent",
        target_listener=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/httpListeners/https-listener"),
        include_path=True,
        include_query_string=True,
    )]
    routing_rules = [
        azure_native.network.ApplicationGatewayRequestRoutingRuleArgs(
            name="http-redirect",
            priority=10,
            rule_type="Basic",
            http_listener=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/httpListeners/http-listener"),
            redirect_configuration=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/redirectConfigurations/redirect-to-https"),
        ),
        azure_native.network.ApplicationGatewayRequestRoutingRuleArgs(
            name="https-route",
            priority=20,
            rule_type="Basic",
            http_listener=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/httpListeners/https-listener"),
            backend_address_pool=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/backendAddressPools/backendPool"),
            backend_http_settings=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/backendHttpSettingsCollection/backendHttpSettings"),
        ),
    ]
else:
    routing_rules = [
        azure_native.network.ApplicationGatewayRequestRoutingRuleArgs(
            name="http-route",
            priority=10,
            rule_type="Basic",
            http_listener=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/httpListeners/http-listener"),
            backend_address_pool=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/backendAddressPools/backendPool"),
            backend_http_settings=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/backendHttpSettingsCollection/backendHttpSettings"),
        )
    ]

# App Gateway identity for Key Vault cert access
ag_identity = identity.id.apply(lambda iid: azure_native.network.ManagedServiceIdentityArgs(
    type="UserAssigned",
    user_assigned_identities={iid: {}},
)) if has_ssl else None

app_gw = azure_native.network.ApplicationGateway(
    app_gw_name,
    resource_group_name=resource_group,
    application_gateway_name=app_gw_name,
    location=azure_location,
    sku=azure_native.network.ApplicationGatewaySkuArgs(
        name="Standard_v2",
        tier="Standard_v2",
        capacity=1,
    ),
    identity=ag_identity,
    gateway_ip_configurations=[azure_native.network.ApplicationGatewayIPConfigurationArgs(
        name="appGatewayIpConfig",
        subnet=azure_native.network.SubResourceArgs(id=app_gw_subnet_id),
    )],
    frontend_ip_configurations=[azure_native.network.ApplicationGatewayFrontendIPConfigurationArgs(
        name="appGwPublicFrontendIp",
        public_ip_address=azure_native.network.SubResourceArgs(id=public_ip.id),
    )],
    frontend_ports=[
        azure_native.network.ApplicationGatewayFrontendPortArgs(name="port_80", port=80),
        azure_native.network.ApplicationGatewayFrontendPortArgs(name="port_443", port=443),
    ],
    backend_address_pools=[
        azure_native.network.ApplicationGatewayBackendAddressPoolArgs(name="backendPool"),
    ],
    backend_http_settings_collection=[
        azure_native.network.ApplicationGatewayBackendHttpSettingsArgs(
            name="backendHttpSettings",
            port=app_port,
            protocol="Http",
            cookie_based_affinity="Disabled",
            request_timeout=600,
            probe=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/probes/healthProbe"),
        )
    ],
    probes=[azure_native.network.ApplicationGatewayProbeArgs(
        name="healthProbe",
        protocol="Http",
        host="127.0.0.1",
        path="/v1/info",
        interval=30,
        timeout=10,
        unhealthy_threshold=3,
    )],
    http_listeners=http_listeners,
    request_routing_rules=routing_rules,
    ssl_certificates=ssl_certs,
    redirect_configurations=redirect_configurations,
    tags=tags,
    opts=pulumi.ResourceOptions(depends_on=[public_ip, kv_access]),
)

# ─────────────────────────────────────────────────────────────────────────────
# VMSS user data — install Docker, clone repo, write .env from Key Vault
# Production: explicit -f docker-compose.yml so the override file is NOT merged
# ─────────────────────────────────────────────────────────────────────────────
user_data_script = f"""#!/bin/bash
set -e
exec > >(tee /var/log/trino-init.log | logger -t trino-init) 2>&1

# startup_code_version={startup_code_version}

apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg-agent software-properties-common git jq unzip python3 apache2-utils

# Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# Docker + Compose
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -
add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable"
apt-get update -y && apt-get install -y docker-ce docker-ce-cli containerd.io
curl -L "https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Fetch secrets from Key Vault using Managed Identity
az login --identity
SECRET_JSON=$(az keyvault secret show --vault-name {key_vault_name} --name {secret_id} --query value -o tsv)

GIT_USER=$(echo "$SECRET_JSON" | jq -r '.git_deploy_username // "x-token-auth"')
GIT_TOKEN=$(echo "$SECRET_JSON" | jq -r '.git_deploy_token')

# Clone repo with credentials injected into URL
REPO_URL="{git_repo_url}"
REPO_PATH=$(echo "$REPO_URL" | sed 's,https://,,')
git clone "https://$GIT_USER:$GIT_TOKEN@$REPO_PATH" /home/ubuntu/trino
git -C /home/ubuntu/trino checkout {git_branch}
git -C /home/ubuntu/trino rev-parse HEAD > /home/ubuntu/trino/.schema-version

# Write .env from all Key Vault secret key=value pairs
echo "$SECRET_JSON" | jq -r 'to_entries[] | .key + "=" + (.value | tostring)' > /home/ubuntu/trino/.env
chmod 600 /home/ubuntu/trino/.env

# Generate Trino config files (coordinator-config.properties, iceberg.properties, password.db)
cd /home/ubuntu/trino
python3 config_generator.py envs_prod.json --env-file .env

# Use explicit file so docker-compose.override.yml (local dev) is NOT merged
docker-compose -f docker-compose.yml up -d --scale trino-worker={worker_count}
"""
custom_data = base64.b64encode(user_data_script.encode()).decode()

# ─────────────────────────────────────────────────────────────────────────────
# Virtual Machine Scale Set (min=max=1 — stateful, single instance)
# ─────────────────────────────────────────────────────────────────────────────
vmss = azure_native.compute.VirtualMachineScaleSet(
    f"{project_name}-vmss{suffix}",
    resource_group_name=resource_group,
    vm_scale_set_name=f"{project_name}-vmss{suffix}",
    location=azure_location,
    sku=azure_native.compute.SkuArgs(
        name=vm_size,
        capacity=1,
        tier="Standard",
    ),
    identity=identity.id.apply(lambda iid: azure_native.compute.VirtualMachineScaleSetIdentityArgs(
        type="UserAssigned",
        user_assigned_identities={iid: {}},
    )),
    upgrade_policy=azure_native.compute.UpgradePolicyArgs(mode="Manual"),
    virtual_machine_profile=azure_native.compute.VirtualMachineScaleSetVMProfileArgs(
        os_profile=azure_native.compute.VirtualMachineScaleSetOSProfileArgs(
            computer_name_prefix=f"{project_name[:9]}{suffix[:3] if suffix else ''}",
            admin_username="ubuntu",
            linux_configuration=azure_native.compute.LinuxConfigurationArgs(
                disable_password_authentication=True,
                ssh=azure_native.compute.SshConfigurationArgs(
                    public_keys=[azure_native.compute.SshPublicKeyArgs(
                        path="/home/ubuntu/.ssh/authorized_keys",
                        key_data=ssh_public_key,
                    )],
                ),
            ),
            custom_data=custom_data,
        ),
        storage_profile=azure_native.compute.VirtualMachineScaleSetStorageProfileArgs(
            image_reference=azure_native.compute.ImageReferenceArgs(
                publisher="Canonical",
                offer="0001-com-ubuntu-server-focal",
                sku="20_04-lts-gen2",
                version="latest",
            ),
            os_disk=azure_native.compute.VirtualMachineScaleSetOSDiskArgs(
                create_option="FromImage",
                disk_size_gb=volume_size,
                managed_disk=azure_native.compute.VirtualMachineScaleSetManagedDiskParametersArgs(
                    storage_account_type="Premium_LRS",
                ),
            ),
        ),
        network_profile=azure_native.compute.VirtualMachineScaleSetNetworkProfileArgs(
            network_interface_configurations=[
                azure_native.compute.VirtualMachineScaleSetNetworkConfigurationArgs(
                    name=f"{project_name}-nic{suffix}",
                    primary=True,
                    ip_configurations=[
                        azure_native.compute.VirtualMachineScaleSetIPConfigurationArgs(
                            name=f"{project_name}-ipconfig{suffix}",
                            subnet=azure_native.compute.ApiEntityReferenceArgs(id=vm_subnet_id),
                            application_gateway_backend_address_pools=[
                                azure_native.network.SubResourceArgs(
                                    id=f"{ag_prefix}/backendAddressPools/backendPool"
                                )
                            ],
                        )
                    ],
                    network_security_group=azure_native.network.SubResourceArgs(id=vm_nsg.id),
                )
            ]
        ),
    ),
    tags=tags,
    opts=pulumi.ResourceOptions(depends_on=[app_gw, kv_access]),
)

# ─────────────────────────────────────────────────────────────────────────────
# Azure DNS — A record → App Gateway public IP
# ─────────────────────────────────────────────────────────────────────────────
azure_native.network.RecordSet(
    f"{project_name}-dns{suffix}",
    resource_group_name=dns_zone_rg,
    zone_name=dns_zone_name,
    relative_record_set_name=dns_record_name,
    record_type="A",
    ttl=30,
    a_records=public_ip.ip_address.apply(
        lambda ip: [azure_native.network.ARecordArgs(ipv4_address=ip)] if ip else []
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# KG Phase 2 — register deployed services (optional)
# ─────────────────────────────────────────────────────────────────────────────
_kg_host     = secrets.get("KG_API_HOST", "")
_kg_token    = secrets.get("KG_API_TOKEN", "")
_kg_mappings = _dwe.get("kg_mappings")

if _kg_host and _kg_token and _kg_mappings:
    import httpx as _httpx
    import warnings as _warnings

    _adapter_name  = _kg_mappings["adapter_name"]
    _kg_props_keys = _kg_mappings.get("kg_adapter_properties", {})
    _kg_outputs    = _kg_mappings.get("kg_pulumi_outputs", {})
    _kg_services   = _kg_mappings.get("services", [])

    _pulumi_export_map = {
        "url":        pulumi.Output.from_input(f"https://{dns_record_name}.{dns_zone_name}"),
        "appgw_name": app_gw.name,
        "vmss_name":  vmss.name,
    }
    _out_names = list(_kg_outputs.keys())
    _out_vals  = [_pulumi_export_map.get(_kg_outputs[n], pulumi.Output.from_input("")) for n in _out_names]

    def _phase2_hydrate(*resolved):
        props = dict(zip(_out_names, resolved))
        for prop, secret_key in _kg_props_keys.items():
            props[prop] = secrets.get(secret_key, "")
        _headers = {"Authorization": f"Bearer {_kg_token}"}
        _base    = _kg_host.rstrip("/")
        try:
            _httpx.patch(
                f"{_base}/adapters/{_adapter_name}/{env}",
                json={"properties": props},
                headers=_headers,
                timeout=10,
            )
        except Exception as _exc:
            _warnings.warn(f"[dwe-kg] PATCH /adapters failed: {_exc}")

        _svc_payloads = []
        for _svc in _kg_services:
            _trigger = _svc.get("trigger_secret", "")
            if _trigger and not secrets.get(_trigger):
                continue
            _svc_props = {p: secrets.get(sk, "") for p, sk in _svc.get("properties", {}).items()}
            _svc_payloads.append({"name": _svc["name"], "properties": _svc_props})

        if _svc_payloads:
            try:
                _httpx.post(
                    f"{_base}/adapters/{_adapter_name}/{env}/services",
                    json={"services": _svc_payloads},
                    headers=_headers,
                    timeout=10,
                )
            except Exception as _exc:
                _warnings.warn(f"[dwe-kg] POST /services failed: {_exc}")

    pulumi.Output.all(*_out_vals).apply(_phase2_hydrate)

# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────
pulumi.export("appgw_name",  app_gw.name)
pulumi.export("vmss_name",   vmss.name)
pulumi.export("url",         f"https://{dns_record_name}.{dns_zone_name}")
pulumi.export("public_ip",   public_ip.ip_address)
pulumi.export("environment", env)
