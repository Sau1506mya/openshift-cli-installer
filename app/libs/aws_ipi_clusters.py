import json
import os
import shlex
import shutil

import click
import shortuuid
import yaml
from clouds.aws.session_clients import s3_client
from jinja2 import DebugUndefined, Environment, FileSystemLoader, meta
from ocp_utilities.utils import run_command

# TODO: enable spot
"""
function inject_spot_instance_config() {
  local dir=${1}

  if [ ! -f /tmp/yq ]; then
    curl -L https://github.com/mikefarah/yq/releases/download/3.3.0/yq_linux_amd64 -o /tmp/yq && chmod +x /tmp/yq
  fi

  PATCH="${SHARED_DIR}/machinesets-spot-instances.yaml.patch"
  cat > "${PATCH}" << EOF
spec:
  template:
    spec:
      providerSpec:
        value:
          spotMarketOptions: {}
EOF

  for MACHINESET in $dir/openshift/99_openshift-cluster-api_worker-machineset-*.yaml; do
    /tmp/yq m -x -i "${MACHINESET}" "${PATCH}"
    echo "Patched spotMarketOptions into ${MACHINESET}"
  done

  echo "Enabled AWS Spot instances for worker nodes"
}
"""


class RunInstallUninstallCommandError(Exception):
    def __init__(self, action, out, err):
        self.action = action
        self.out = out
        self.err = err

    def __str__(self):
        return f"Failed to run cluster {self.action}\nERR: {self.err}\nOUT: {self.out}"


def create_install_config_file(clusters, pull_secret_file):
    ssh_key = get_local_ssh_key()
    pull_secret = json.dumps(get_pull_secret_data(pull_secret_file=pull_secret_file))
    for _cluster in clusters:
        install_dir = _cluster["install-dir"]
        _cluster["pull_secret"] = pull_secret
        _cluster["ssh_key"] = ssh_key

        cluster_install_config = get_install_config_j2_template(cluster_dict=_cluster)

        with open(os.path.join(install_dir, "install-config.yaml"), "w") as fd:
            fd.write(yaml.dump(cluster_install_config))

    return clusters


def get_pull_secret_data(pull_secret_file):
    with open(pull_secret_file) as fd:
        return json.load(fd)


def get_local_ssh_key():
    with open(os.path.expanduser("~/.ssh/id_rsa.pub")) as fd:
        return fd.read().strip()


def get_install_config_j2_template(cluster_dict):
    template_file = "install-config-template.j2"
    env = Environment(
        loader=FileSystemLoader("app/manifests/"),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=DebugUndefined,
    )

    template = env.get_template(name=template_file)
    rendered = template.render(cluster_dict)
    undefined_variables = meta.find_undeclared_variables(env.parse(rendered))
    if undefined_variables:
        click.echo(f"The following variables are undefined: {undefined_variables}")
        raise click.Abort()

    return yaml.safe_load(rendered)


def download_openshift_install_binary(clusters, pull_secret_file):
    versions = set()
    openshift_install_str = "openshift-install"

    for cluster in clusters:
        versions.add(cluster["version"])

    for version in versions:
        binary_dir = os.path.join("/tmp", version)
        clusters = [_cluster for _cluster in clusters if _cluster["version"] == version]
        for cluster in clusters:
            cluster["openshift-install-binary"] = os.path.join(
                binary_dir, openshift_install_str
            )

        run_command(
            command=shlex.split(
                "oc adm release extract "
                f"quay.io/openshift-release-dev/ocp-release:{version}-x86_64 "
                f"--command={openshift_install_str} --to={binary_dir} --registry-config={pull_secret_file}"
            ),
            check=False,
        )

    return clusters


def create_or_destroy_aws_ipi_cluster(
    cluster_data, action, s3_bucket_name=None, s3_bucket_path=None
):
    directory = cluster_data["install-dir"]
    binary_path = cluster_data["openshift-install-binary"]
    res, out, err = run_command(
        command=shlex.split(f"{binary_path} {action} cluster --dir {directory}"),
        capture_output=False,
        check=False,
    )
    if action == "create" and s3_bucket_name:
        zip_file = shutil.make_archive(
            base_name=f"{directory}-{shortuuid.uuid()}",
            format="zip",
            root_dir=directory,
        )
        s3_client().upload_file(
            Filename=zip_file,
            Bucket=s3_bucket_name,
            Key=os.path.join(s3_bucket_path or "", os.path.split(zip_file)[-1]),
        )
    if not res:
        raise RunInstallUninstallCommandError(action=action, out=out, err=err)
