"""
One way to open a Snowflake connection, used by deploy.py, the CI scripts and the integration tests.

Two ways to log in, nothing stored in the repo:
  1. On your laptop:   a named connection in ~/.snowflake/connections.toml   ->  connect(connection_name="nbo_rebuild")
  2. In GitHub Actions: environment variables from GitHub Secrets (key pair, no password)
        SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY (the PEM text) or SNOWFLAKE_PRIVATE_KEY_PATH,
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE (optional), SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE
"""
import os

import snowflake.connector
from cryptography.hazmat.primitives import serialization


def _private_key_der():
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    if not pem and path:
        with open(path, "rb") as f:
            pem = f.read().decode()
    if not pem:
        return None
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    key = serialization.load_pem_private_key(pem.encode(), password=passphrase.encode() if passphrase else None)
    return key.private_bytes(encoding=serialization.Encoding.DER, format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


def connect(connection_name=None, schema=None, query_tag="nbo"):
    """Open a connection. connection_name -> ~/.snowflake/connections.toml, otherwise environment variables."""
    params = dict(session_parameters={"QUERY_TAG": query_tag})
    if schema:
        params["schema"] = schema
    if connection_name:
        return snowflake.connector.connect(connection_name=connection_name, **params)
    key = _private_key_der()
    if key is None:
        raise RuntimeError("No Snowflake login: give --connection NAME or set SNOWFLAKE_PRIVATE_KEY(_PATH)")
    missing = [v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE",
                           "SNOWFLAKE_DATABASE") if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"], private_key=key,
        role=os.environ["SNOWFLAKE_ROLE"], warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"], **params)
