# Set up docs-mcp on Azure Container Apps

How to run docs-mcp in Azure for your users, with sign-in through Entra ID and
HTTPS. It takes about an hour, plus whatever your organization needs to approve
a domain name, an Entra ID app and an Azure DevOps account.

> **Status:** written from Azure's documentation and the settings docs-mcp is
> tested with. It has not yet been run end to end. If a command or field
> differs from what Azure accepts, fix it here.

Azure Container Apps runs a container without a Kubernetes cluster to look
after. It terminates TLS, holds secrets, pulls images with a managed identity,
and can be kept inside your network. If you already run AKS, use the Kubernetes
example in `deploy/k8s` instead.

## What you will set up

![Users reach docs-mcp over HTTPS with a token from Entra ID. docs-mcp runs as a container app, pulls the handbook from Azure DevOps, and fetches Entra ID's signing keys. A managed identity pulls the image from the registry and reads secrets from Key Vault. Logs go to Log Analytics.](figures/azure-container-apps.svg)

| Piece | What it is for |
|---|---|
| **Container registry** | Holds the docs-mcp image |
| **Managed identity** | Pulls the image and reads secrets, without any password |
| **Key Vault** | The git token, the refresh secret, and for an internal setup the certificate |
| **Container Apps environment** | Decides who can reach the app. Sends logs to Log Analytics |
| **Container app** | docs-mcp itself |
| **Custom domain and certificate** | The address users connect to, such as `docs-mcp.example.com` |
| **Entra ID app registration** | The sign-in ([Sign-in with Entra ID](sso-entra.md)) |
| **Azure DevOps account** | Read access to the handbook, and nothing else |

## Decide three things first

**1. Internal or public?**

| | Internal: only on your network | Public: on the internet, behind the sign-in |
|---|---|---|
| Who can reach it | People on the corporate network or VPN | Anyone. Entra ID decides who gets in, including Conditional Access: MFA, managed devices |
| Push webhook from Azure DevOps | Cannot reach it. docs-mcp pulls every 60 seconds instead | Works: updates within seconds |
| Certificate | Your own, from Key Vault | Free, managed by Azure |
| Health endpoints | Reachable from your network | Reachable by anyone. They show page count, commit and the last sync error, no content |
| Needs | A subnet in a virtual network that users can reach | Nothing extra |

Internal matches how docs-mcp was designed: one service on the internal
network. Public suits people who work without VPN, and is reasonable now that
every request needs a sign-in.

**2. The host name.** Users connect to `https://<host name>/mcp`, and Entra ID
accepts that as the app's identifier only on a domain verified in your tenant.
So the default `*.azurecontainerapps.io` name will not do. Pick a name such as
`docs-mcp.example.com` on a verified domain, and make sure you can add DNS
records for it.

**3. Rust or Python.** Both images behave the same, and both fit the smallest
size Container Apps offers.

| | Rust | Python |
|---|---|---|
| Start to ready | 0.5 s | 3.1 s |
| Memory | 11 MB | 64 MB |

Choose the language your team would rather maintain. The commands below use
Rust; for Python, use `docs-mcp-python` and `python/` instead.

## The names used below

```sh
RG=rg-docs-mcp
LOC=<your-region>
ACR=acrdocsmcp               # globally unique, letters and digits only
KV=kv-docs-mcp               # globally unique
ID=id-docs-mcp
ENV=cae-docs-mcp
APP=docs-mcp
HOST=docs-mcp.example.com    # on a domain verified in Entra ID
IMAGE=docs-mcp-rust          # or docs-mcp-python
VERSION=0.1.0
```

## 1. An Azure DevOps account for docs-mcp

docs-mcp reads the handbook with a personal access token (PAT). On Azure
DevOps, a PAT reaches every repo its account can see, so create one for an
account that can see only the handbook.

1. Create a dedicated user, such as `svc-docs-mcp@example.com`, and add it to
   the Azure DevOps organization with *Basic* access.
2. Give it read access to the handbook repo, and nothing else. Check under
   *Project settings → Repositories → Security* that it cannot read other repos.
3. Sign in as that user and create a PAT: *Code: Read*, for this organization
   only. Its expiry date is your next rotation date. Put it in a calendar.

Keep the PAT for step 4.

## 2. Register docs-mcp in Entra ID

Follow [Sign-in with Entra ID, step 1](sso-entra.md#1-register-docs-mcp-in-entra-id),
with `https://<host name>/mcp` as the Application ID URI. You will need:

```sh
TENANT_ID=<Directory (tenant) ID>
CLIENT_ID=<Application (client) ID>
```

## 3. The registry and the image

From the root of this repository:

```sh
az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic
az acr build -r $ACR -t $IMAGE:$VERSION -f rust/Containerfile rust
```

`az acr build` builds in Azure, so you need no container tools locally. Tag
every build with a version, never `latest`: a new version is how you roll out,
and an old one is how you roll back.

## 4. The identity and the secrets

One user-assigned identity pulls the image and reads the secrets. It exists
before the app does, so its access can be granted first.

```sh
az identity create -g $RG -n $ID
ID_RESOURCE=$(az identity show -g $RG -n $ID --query id -o tsv)
ID_PRINCIPAL=$(az identity show -g $RG -n $ID --query principalId -o tsv)

az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope $(az acr show -n $ACR --query id -o tsv)

az keyvault create -g $RG -n $KV --enable-rbac-authorization true
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope $(az keyvault show -n $KV --query id -o tsv)

az keyvault secret set --vault-name $KV -n git-token --value "<the PAT from step 1>"
az keyvault secret set --vault-name $KV -n refresh-token --value "$(openssl rand -hex 32)"
```

Setting the secrets needs *Key Vault Secrets Officer* on the vault for your own
account. The refresh secret is for the push webhook (step 8). Keep it in Key
Vault; you will paste it into Azure DevOps once.

## 5. The environment

**Internal.** You need an empty subnet, `/27` or larger, delegated to
`Microsoft.App/environments`, in a virtual network your users can reach.

```sh
SUBNET_ID=<the subnet's resource ID>
az containerapp env create -g $RG -n $ENV -l $LOC \
  --infrastructure-subnet-resource-id $SUBNET_ID --internal-only true
```

The environment needs outbound HTTPS to `dev.azure.com` and
`login.microsoftonline.com`. If the network has a firewall, allow both.

**Public:**

```sh
az containerapp env create -g $RG -n $ENV -l $LOC
```

Either way, Azure creates a Log Analytics workspace for the logs.

## 6. The container app

Health probes can only be set in YAML, so the app is described in a file.
Save this as `docs-mcp.yaml`, and replace every `<…>`:

```yaml
location: <LOC>
identity:
  type: UserAssigned
  userAssignedIdentities:
    <ID_RESOURCE>: {}
properties:
  managedEnvironmentId: <the environment's resource ID: az containerapp env show -g $RG -n $ENV --query id -o tsv>
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true        # in an internal environment: reachable from your network, not the internet
      targetPort: 8080
      transport: http
      allowInsecure: false  # HTTPS only
    registries:
      - server: <ACR>.azurecr.io
        identity: <ID_RESOURCE>
    secrets:
      - name: git-token
        keyVaultUrl: https://<KV>.vault.azure.net/secrets/git-token
        identity: <ID_RESOURCE>
      - name: refresh-token
        keyVaultUrl: https://<KV>.vault.azure.net/secrets/refresh-token
        identity: <ID_RESOURCE>
  template:
    containers:
      - name: docs-mcp
        image: <ACR>.azurecr.io/<IMAGE>:<VERSION>
        resources:
          cpu: 0.25
          memory: 0.5Gi
        env:
          # The handbook
          - { name: DOCS_REPO_URL, value: "https://dev.azure.com/<ORG>/<PROJECT>/_git/<REPO>" }
          - { name: DOCS_REF, value: main }
          - { name: DOCS_DIR, value: src/content/docs }
          - { name: DOCS_NAME, value: dev-handbook }
          - { name: DOCS_LANGUAGE, value: Norwegian Nynorsk }
          - { name: DOCS_SITE_URL, value: "https://<the rendered handbook>/" }
          - { name: DOCS_REFRESH_SECONDS, value: "60" }
          - { name: DOCS_GIT_TOKEN, secretRef: git-token }
          # Who may connect
          - { name: MCP_ALLOWED_HOSTS, value: <HOST> }
          - { name: MCP_PUBLIC_URL, value: "https://<HOST>/mcp" }
          - { name: MCP_OAUTH_ISSUER, value: "https://login.microsoftonline.com/<TENANT_ID>/v2.0" }
          - { name: MCP_OAUTH_AUDIENCE, value: <CLIENT_ID> }
          - { name: MCP_OAUTH_SCOPES, value: "https://<HOST>/mcp/docs.read" }
          - { name: MCP_REFRESH_TOKEN, secretRef: refresh-token }
        probes:
          - type: Startup       # the first clone
            httpGet: { path: /readyz, port: 8080 }
            periodSeconds: 10
            failureThreshold: 10
          - type: Readiness     # traffic only once the index is loaded
            httpGet: { path: /readyz, port: 8080 }
            periodSeconds: 10
          - type: Liveness
            httpGet: { path: /healthz, port: 8080 }
            periodSeconds: 20
    scale:
      minReplicas: 1
      maxReplicas: 2
```

```sh
az containerapp create -g $RG -n $APP --yaml docs-mcp.yaml
```

Why these values:

- **`minReplicas: 1`.** At zero, the first question after a quiet spell would
  wait for a fresh clone.
- **`DOCS_REFRESH_SECONDS: 60`.** With two replicas, the push webhook reaches
  only one of them. The other catches up on its next pull.
- **`MCP_ALLOWED_HOSTS` is only your host name.** Requests to the default
  `*.azurecontainerapps.io` name are refused. That is on purpose: sign-in only
  works on your host name anyway. Add the default name for a while if you want
  to test before the domain is set up.
- **Do not turn on the app's built-in *Authentication*** (Easy Auth) in the
  portal. It would send clients to a login page, and MCP clients need
  docs-mcp's own `401`, which tells them where to sign in.

**More than one repo**, such as pipeline templates next to the handbook:
replace the `DOCS_REPO_URL`, `DOCS_REF`, `DOCS_DIR`, `DOCS_LANGUAGE` and
`DOCS_SITE_URL` lines with one `DOCS_SOURCES`, described in the README's
[Several sources](../README.md#several-sources):

```yaml
          - name: DOCS_SOURCES
            value: |
              - name: handbook
                description: The dev handbook. Rules and guidance for Rust, Python and our workflow.
                repo: https://dev.azure.com/<ORG>/<PROJECT>/_git/dev-handbook
                dir: src/content/docs
                language: Norwegian Nynorsk
              - name: ci-templates
                description: Azure DevOps pipeline templates. Use them when writing azure-pipelines.yml.
                repo: https://dev.azure.com/<ORG>/<PROJECT>/_git/ci-templates
                include: ["**/*.yml", "**/*.yaml", "**/*.md"]
```

The Azure DevOps account from step 1 then needs read access to every listed
repo, and still nothing else.

**Without sign-in**, with a shared token instead: drop the four `MCP_OAUTH_*`
and `MCP_PUBLIC_URL` lines, store a long random token in Key Vault, and pass it
as `MCP_AUTH_TOKEN` the same way as the git token.

## 7. The domain and the certificate

**Public:** a free certificate managed by Azure.

```sh
FQDN=$(az containerapp show -g $RG -n $APP --query properties.configuration.ingress.fqdn -o tsv)
VERIFY=$(az containerapp show -g $RG -n $APP --query properties.customDomainVerificationId -o tsv)
```

Add two DNS records: a `CNAME` from your host name to `$FQDN`, and a `TXT`
record named `asuid.<host name>` with the value `$VERIFY`. Then:

```sh
az containerapp hostname add -g $RG -n $APP --hostname $HOST
az containerapp hostname bind -g $RG -n $APP --hostname $HOST --environment $ENV --validation-method CNAME
```

**Internal:** your own certificate, from Key Vault. Azure cannot issue one for
a name it cannot reach.

1. Get a certificate for the host name from your internal or public CA, as a
   PFX, with RSA or ECDSA P-256 keys. Users' machines must trust its CA.
2. Import it into Key Vault (*Certificates → Generate/Import*), as
   `docs-mcp-cert`.
3. Give the environment the identity, and import the certificate:

   ```sh
   az containerapp env identity assign -g $RG -n $ENV --user-assigned $ID_RESOURCE
   az containerapp env certificate upload -g $RG -n $ENV --certificate-name docs-mcp-cert \
     --akv-url https://$KV.vault.azure.net/secrets/docs-mcp-cert --certificate-identity $ID_RESOURCE
   ```

4. Point your host name at the environment in your internal DNS: an `A` record
   to `az containerapp env show -g $RG -n $ENV --query properties.staticIp -o tsv`.
5. Bind it:

   ```sh
   az containerapp hostname add -g $RG -n $APP --hostname $HOST
   az containerapp hostname bind -g $RG -n $APP --hostname $HOST --environment $ENV --certificate docs-mcp-cert
   ```

When you renew the certificate in Key Vault, Container Apps picks it up within
12 hours.

## 8. Keeping it up to date

**Public:** add a push webhook, so a change reaches docs-mcp within seconds.
In Azure DevOps, *Project settings → Service hooks → + → Web Hooks*:

- Trigger: *Code pushed*, for the handbook repo and the served branch.
- URL: `https://<host name>/refresh`
- HTTP headers: `Authorization: Bearer <the refresh-token secret>`
- *Test* should answer `200`.

**Internal:** nothing to do. Azure DevOps cannot reach into your network, and
docs-mcp pulls every 60 seconds.

## 9. Check it

```sh
curl https://$HOST/.well-known/oauth-protected-resource/mcp
curl -i -X POST https://$HOST/mcp
az containerapp logs show -g $RG -n $APP --tail 50
```

- The first returns JSON with your tenant in `authorization_servers`.
- The second returns `401`, with a `WWW-Authenticate` header that points at
  the first.
- The log shows `auth: oauth (…)` at startup and `indexed … pages at …` after
  the first clone, and no warnings. Questions are never logged.

## 10. Connect the users

In VS Code, `.vscode/mcp.json` needs only the address. VS Code finds the
sign-in by itself:

```json
{ "servers": { "dev-handbook": { "type": "http", "url": "https://docs-mcp.example.com/mcp" } } }
```

**GitHub Copilot needs an admin to allow it.** On Copilot Business or
Enterprise, an organization owner must enable the *MCP servers in Copilot*
policy. If your enterprise limits Copilot to a list of approved MCP servers,
add docs-mcp to it.

Copilot CLI and the JetBrains plugin need a client ID of their own, and have
open bugs with Entra ID. Run the [test checklist](sso-entra.md#4-test-it) for
each client before you tell its users to switch.

## Running it

| Task | How |
|---|---|
| **Roll out a new version** | `az acr build … -t $IMAGE:<new version>`, then `az containerapp update -g $RG -n $APP --image $ACR.azurecr.io/$IMAGE:<new version>` |
| **Roll back** | The same, with the previous version |
| **Rotate the PAT** | Create a new one (step 1). `az keyvault secret set --vault-name $KV -n git-token --value …`. Then restart: `az containerapp revision restart -g $RG -n $APP --revision $(az containerapp show -g $RG -n $APP --query properties.latestRevisionName -o tsv)`. The container reads its secrets when it starts |
| **See why the handbook is stale** | `/readyz` from inside the network, or the log: `last_error` holds git's own message |
| **Logs** | Log Analytics, table `ContainerAppConsoleLogs_CL` |
| **Patch** | Rebuild the image every few weeks, even without changes: most of it is git and Debian, which get security fixes |

## When something is wrong

| Symptom | Usual cause |
|---|---|
| The image cannot be pulled | The identity lacks `AcrPull`, or `registries.identity` names another identity |
| A secret cannot be fetched from Key Vault | The identity lacks *Key Vault Secrets User*, or the vault's firewall blocks the environment |
| The revision keeps restarting; the log says `git clone failed: … Authentication failed` | The PAT is wrong, has expired, or its account cannot read the repo |
| The log says `git clone failed: … 302` | `DOCS_REPO_URL` is not the repo's clone URL. Copy it from *Clone* in Azure DevOps |
| `403 Forbidden: host not allowed` | `MCP_ALLOWED_HOSTS` lacks the name used. The log names it |
| `503 … cannot reach the OAuth issuer` | No outbound access to `login.microsoftonline.com` |
| `401` with `wrong audience`, `wrong issuer`, `missing scope`, or an `AADSTS…` error | The Entra ID setup. See [When something fails](sso-entra.md#4-test-it) |
| The domain will not bind (public) | The `CNAME` or `asuid` `TXT` record is missing, or has not reached DNS yet |

## How this differs from the Kubernetes example

| | Kubernetes example | Container Apps |
|---|---|---|
| TLS | The ingress, with your certificate | Built in |
| Paths reachable | Only `/mcp`, `/refresh` and the OAuth metadata | All, including `/healthz` and `/readyz` ([threat model](threat-model.md#findings), F8) |
| Root filesystem | Read-only | Writable; it cannot be made read-only. The image still runs as an unprivileged user |
| Secrets | Kubernetes secrets | Key Vault, through the managed identity |
| Outbound traffic | A network policy, if you add one | The subnet's network security group or firewall (internal) |
