"""Create local secrets and loopback-only configuration without logging credentials."""

import json
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values

project = Path(__file__).resolve().parent.parent
runtime = project / ".local" / "napcat-node"
config = runtime / "napcat" / "config"
config.mkdir(parents=True, exist_ok=True)
webui = config / "webui.json"
if not webui.exists():
    webui.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 6099,
                "token": secrets.token_urlsafe(32),
                "autoLoginAccount": "",
                "disableWebUI": False,
            }
        ),
        encoding="utf-8",
    )
env = project / ".env"
if not env.exists():
    roots = [
        runtime,
        Path.home() / "Documents" / "Tencent Files",
        Path.home() / "Documents" / "QQ Files",
        Path.home() / "AppData" / "Roaming" / "Tencent",
        Path.home() / "AppData" / "Local" / "Tencent",
    ]
    env.write_text(
        "\n".join(
            [
                "NAPCAT_URL=http://127.0.0.1:3000",
                f"NAPCAT_TOKEN={secrets.token_urlsafe(32)}",
                f"QQ_FILE_STATE_DIR={project / '.local' / 'state'}",
                f"QQ_FILE_DOWNLOAD_DIR={Path.home() / 'Downloads' / 'QQ-File-MCP'}",
                "QQ_FILE_ALLOWED_ROOTS=" + os.pathsep.join(str(p) for p in roots),
                "",
            ]
        ),
        encoding="utf-8",
    )
token = dotenv_values(env).get("NAPCAT_TOKEN")
if not token:
    raise SystemExit("Existing .env must contain NAPCAT_TOKEN; it was not overwritten.")
onebot_path = config / "onebot11.json"
if not onebot_path.exists():
    onebot_path.write_text(
        json.dumps(
            {
                "network": {
                    "httpServers": [
                        {
                            "name": "qq-file-mcp",
                            "enable": True,
                            "host": "127.0.0.1",
                            "port": 3000,
                            "token": token,
                            "enableCors": False,
                            "messagePostFormat": "array",
                            "debug": False,
                        }
                    ]
                },
                "enableLocalFile2Url": False,
                "parseMultMsg": False,
            }
        ),
        encoding="utf-8",
    )
print("Local runtime configured; secrets are stored in ignored files only.")
