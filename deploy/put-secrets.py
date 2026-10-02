"""Write production secrets to SSM without passing values on command lines."""
import argparse
import getpass
import secrets
import subprocess
from cryptography.fernet import Fernet

NAMES = ("google-client-id", "google-client-secret", "credential-encryption-key", "postgres-password", "anthropic-api-key")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="/assistant-agent")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    for name in NAMES:
        default = Fernet.generate_key().decode() if name == "credential-encryption-key" else (secrets.token_hex(32) if name == "postgres-password" else "")
        value = getpass.getpass(f"{name} (Enter to generate when available): ") or default
        if not value:
            raise SystemExit(f"{name} is required")
        subprocess.run(["aws", "ssm", "put-parameter", "--region", args.region, "--name", f"{args.prefix}/{name}", "--type", "SecureString", "--overwrite", "--value", "file:///dev/stdin"], input=value, text=True, check=True, stdout=subprocess.DEVNULL)
        print(f"Stored {args.prefix}/{name}")


if __name__ == "__main__":
    main()
