# Deploy SignalWatch to staging

Staging runs one SignalWatch app, one DBOS worker, and one changedetection.io instance on a private x86_64 VM. A push to `main` builds the exact commit on a GitHub-hosted runner and sends the compressed image, Compose file, and deploy script to the VM over IAP. GitHub queues deployments and runs one at a time.

## 1. Provision the staging VM

1. Use a separate GCP project with billing, a staging VPC and subnet, and one x86_64 Ubuntu VM. Enable the Compute Engine, IAP, OS Login, IAM, IAM Service Account Credentials, Security Token Service, and Cloud Resource Manager APIs. Enable OS Login on the VM (`enable-oslogin=TRUE`). Create it without an external IP or an attached service account.
2. Target an ingress firewall rule at this VM that allows TCP `22` from `35.235.240.0/20`. Remove broader ingress rules that apply to it. Provide outbound DNS and network access using Cloud NAT or your project’s equivalent, including OS and Docker updates, the changedetection.io image registry, monitored websites and feeds, and the OpenAI API. [IAP setup](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding)
3. Attach a dedicated, non-auto-delete Persistent Disk in the VM’s zone. Format it only on initial setup. Mount it at `/var/lib/signalwatch` and add its filesystem UUID to `/etc/fstab` so it mounts at boot. Verify with `findmnt --mountpoint /var/lib/signalwatch`. Keep the disk when replacing the VM. [Persistent Disk mounting](https://docs.cloud.google.com/compute/docs/disks/add-persistent-disk#format_mount)
4. Install Docker Engine and the Docker Compose plugin. Set Docker to start after the data disk mounts by adding a drop-in with `sudo systemctl edit docker.service`. Run `sudo systemctl daemon-reload`, `sudo systemctl enable docker.service`, and `sudo systemctl restart docker.service`. Verify `sudo docker compose version`. [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)

   ```ini
   [Unit]
   RequiresMountsFor=/var/lib/signalwatch
   ```

## 2. Give GitHub access to the staging VM

Create a dedicated Workload Identity Pool and a dedicated service account in the staging project. The example uses the numeric GitHub ID for `yaelmi3/signalwatch`. To confirm it, run `gh api repos/yaelmi3/signalwatch --jq .id`.

```bash
PROJECT_ID='your-staging-project-id'
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
REPO_ID=1460007321
POOL=github-actions
PROVIDER=github
SA_NAME=signalwatch-staging-deploy
SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT_ID"
gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT_ID" --location=global
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping='google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref,attribute.environment=assertion.environment,attribute.workflow_ref=assertion.workflow_ref' \
  --attribute-condition="attribute.repository_id == '${REPO_ID}' && attribute.ref == 'refs/heads/main' && attribute.environment == 'staging' && attribute.workflow_ref == 'yaelmi3/signalwatch/.github/workflows/deploy-staging.yml@refs/heads/main'"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPO_ID"
```

The provider checks the repository, branch, environment, and workflow. The repository principal can impersonate only the dedicated service account through this pool. [Google Cloud federation for GitHub Actions](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)

Grant the service account the following permissions. Give the operator equivalent login and tunnel access for bootstrap and maintenance.

- On the VM’s **IAP TCP resource** in **IAP → SSH and TCP Resources**, add **IAP-secured Tunnel User** (`roles/iap.tunnelResourceAccessor`) with the condition `destination.port == 22`.
- On the **Compute Engine instance**, grant **Compute OS Admin Login** (`roles/compute.osAdminLogin`) so the deploy script can run under `sudo`.
- On the **staging project**, grant a custom role with `compute.projects.get` and `compute.instances.list` for gcloud’s SSH and SCP lookups. The instance binding already grants `compute.instances.get` for this VM. [IAP permissions](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding#permissions_details) · [OS Login permissions](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin)

## 3. Bootstrap changedetection.io and the secrets

On the VM, verify the data disk is mounted, then create the private files once. The command generates the shared callback token and writes it to both files.

```bash
findmnt --mountpoint /var/lib/signalwatch
sudo install -d -m 0750 /opt/signalwatch
sudo bash <<'SETUP'
set -euo pipefail
umask 077
set -o noclobber
token="$(openssl rand -hex 32)"
printf 'CHANGEDETECTION_WEBHOOK_SECRET=%s\n' "$token" > /opt/signalwatch/staging.env
printf 'machine signalwatch-hook\n  login signalwatch\n  password %s\n' "$token" > /opt/signalwatch/changedetection.netrc
SETUP
```

On your workstation, set `VM` and `ZONE` to the instance name and zone. Build and copy the detector proxy with `compose.yaml`.

```bash
docker build --platform linux/amd64 --target detector-egress --tag signalwatch-detector-egress:0.1.0 .
docker save signalwatch-detector-egress:0.1.0 | gzip > /tmp/signalwatch-detector-egress.tar.gz
gcloud compute scp --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap \
  compose.yaml /tmp/signalwatch-detector-egress.tar.gz "$VM:~/"
```

From a shell on the VM, start changedetection.io and its proxy against the persistent disk. The temporary values satisfy Compose interpolation for the app and worker, which start on the first deploy.

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /var/lib/signalwatch/app
sudo install -d -m 0700 /var/lib/signalwatch/changedetection
gzip -dc "$HOME/signalwatch-detector-egress.tar.gz" | sudo docker load
sudo env SIGNALWATCH_IMAGE=signalwatch:bootstrap SIGNALWATCH_STATE_DIR=/var/lib/signalwatch \
  CHANGEDETECTION_API_KEY=bootstrap OPENAI_API_KEY=bootstrap \
  SIGNALWATCH_ANALYSIS_MODEL=bootstrap SIGNALWATCH_ANALYSIS_HIGHER_QUALITY_MODEL=bootstrap \
  docker compose --project-name signalwatch --env-file /opt/signalwatch/staging.env \
  --file "$HOME/compose.yaml" \
  up -d changedetection
```

From your workstation, forward both loopback ports over IAP SSH. Keep the session open while using the web interfaces.

```bash
gcloud compute ssh "$VM" --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap -- \
  -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8001:127.0.0.1:8001 -L 127.0.0.1:5000:127.0.0.1:5000
```

Open [changedetection.io](http://127.0.0.1:5000), enable API access in **Settings → API** and copy its API key. On the VM, use `sudoedit /opt/signalwatch/staging.env` to add the four entries below. Leave the generated `CHANGEDETECTION_WEBHOOK_SECRET` entry in place. Choose two models with structured outputs. Keep both files owned by `root:root` with mode `0600`. [changedetection.io API documentation](https://changedetection.io/docs/api_v1/index.html)

```dotenv
CHANGEDETECTION_API_KEY=<changedetection.io key>
OPENAI_API_KEY=<staging OpenAI key>
SIGNALWATCH_ANALYSIS_MODEL=<economy model>
SIGNALWATCH_ANALYSIS_HIGHER_QUALITY_MODEL=<higher quality model>
```

The app reaches the engine at `http://changedetection:5000`. The engine uses `NETRC` to authenticate to `http://signalwatch-hook:8000/api/webhooks/changedetection` within the Compose network.

## 4. Enable deployments and check the first run

Create the GitHub Environment `staging` and restrict deployments to `main` in its deployment branch policy. Set these as **environment variables** in GitHub’s **Settings → Environments → staging**.

| Variable | Value |
| --- | --- |
| `GCP_PROJECT_ID` | Staging project ID |
| `GCP_ZONE` | VM zone, such as `us-central1-a` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-actions/providers/github` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `signalwatch-staging-deploy@PROJECT_ID.iam.gserviceaccount.com` |
| `GCP_VM` | Exact VM instance name. Set this last to enable deployment. |

If setup is finished when the workflow first merges to `main`, that push deploys staging. If it merges first, complete setup and select **Actions → Deploy staging → Run workflow → main**. The workflow skips deployment while `GCP_VM` is unset. Once set, any missing or malformed setting fails the run before it uploads files.

Watch the workflow through rollout. Open [SignalWatch](http://127.0.0.1:8001) over the SSH tunnel and use **Configuration → Maintainer settings** to check the webhook and analysis settings. On the VM, `sudo docker ps --filter label=com.docker.compose.project=signalwatch` should list the app, one worker, and changedetection.io.

To retry, fix the reported error and dispatch from `main` again. Re-running a failed GitHub run deploys that run’s original commit, so use that option when it is still the release you intend to deploy. If the remote rollout already started, check the running containers and logs before retrying.

## 5. Enable Slack delivery (optional)

In the Slack app settings, register the exact HTTPS callback URL for this deployment. For the public SignalWatch site, use `https://signalwatch-q5xrkk4n7a-uc.a.run.app/api/maintainer/slack/callback`. Exclude callback query strings from gateway and hosting access logs before connecting Slack.

Keep the values in Secret Manager under `signalwatch-slack-client-secret`, `signalwatch-slack-maintainer-passphrase`, and `signalwatch-slack-encryption-key`. Install the settings below from Secret Manager and the Slack app configuration into `/opt/signalwatch/slack.env` on the VM. Set owner `root:root` and mode `0600`. The app and worker both load this optional file.

```dotenv
SIGNALWATCH_SLACK_CLIENT_ID=<Slack app client ID>
SIGNALWATCH_SLACK_CLIENT_SECRET=<existing Slack app client secret>
SIGNALWATCH_SLACK_REDIRECT_URL=https://signalwatch-q5xrkk4n7a-uc.a.run.app/api/maintainer/slack/callback
SIGNALWATCH_SLACK_MAINTAINER_PASSWORD=<strong random Slack-only passphrase>
SIGNALWATCH_SLACK_ENCRYPTION_KEY=<private Fernet key>
```

After deploying from `main`, check that `GET /api/maintainer/slack` reports `available: true`. To connect a workspace, open **Maintainer settings → Slack delivery** and use the setup passphrase. Once connected, anyone who can open the site can select public Slack channels, enable delivery, and send test messages.

## 6. Alert when monitoring goes stale

In Cloud Monitoring, create an HTTPS uptime check for the SignalWatch hostname and path `/api/monitoring-health`.
Set the check interval to five minutes and require HTTP 200. Enable an alert policy for failed checks and select
the maintainer's email notification channel. [Uptime check setup](https://cloud.google.com/monitoring/uptime-checks)

The status URL returns 503 when a watched page or RSS feed is stale. It returns only an HTTP status, so the
checker can use the public gateway. An unreachable app also fails the check. Open **Maintainer settings →
Source monitoring** to see which sources are stale and when they last checked successfully.

RSS feeds become stale after two hours without a successful check. Watched pages get four hours, which allows for
changedetection.io's default three-hour schedule. If you extend that schedule, set `SIGNALWATCH_PAGE_STALE_SECONDS`
in `staging.env` to a window longer than your slowest watch interval.

## Back up and roll back

Clear `GCP_VM` and let any active deployment finish before maintenance. For upgrades that change DBOS or its workflows, follow the [worker upgrade procedure](../README.md#worker-upgrades-and-retention) using the previous release. The deploy script checks for unfinished jobs and saves SQLite backups under `/var/lib/signalwatch/backups` before switching releases.

For a full backup, stop the app, worker, and changedetection.io in the `signalwatch` Compose project. Take a data disk snapshot or make an offline copy to separate storage. Include `changedetection/` and `app/` with both SQLite databases and any sidecar files. Store protected copies of `staging.env` and `changedetection.netrc` separately. [Persistent Disk snapshots](https://docs.cloud.google.com/compute/docs/disks/create-snapshots)

For a manual rollback, select a known healthy SHA. After a failed rollout, `/opt/signalwatch/current-sha` identifies the last release that passed its health check. Confirm the prior release can use the current application and DBOS schemas, pending workflows, and engine datastore. If incompatible, stop all services and restore the matching pre-deploy backup of both SQLite databases and the changedetection.io store together. Restoring that backup discards newer writes.

Use the prior release’s Compose file and loaded image. Replace `SHA` with the full commit SHA.

```bash
SHA='previous-full-commit-sha'
sudo env SIGNALWATCH_IMAGE="signalwatch:$SHA" SIGNALWATCH_STATE_DIR=/var/lib/signalwatch \
  docker compose --project-name signalwatch --env-file /opt/signalwatch/staging.env \
  --file "/opt/signalwatch/releases/$SHA/compose.yaml" up -d --pull never --wait --wait-timeout 120
```

Verify all three services. Set `GCP_VM` again when `main` is ready to deploy.
