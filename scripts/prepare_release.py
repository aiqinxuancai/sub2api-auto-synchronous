"""Validate a version tag and render deployment files for this GitHub repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?")


def release_info(repository, tag, project_file=ROOT / "pyproject.toml"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub repository must have the form owner/repository")
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("Use a version tag such as v0.4.0 or v0.4.0-rc.1")
    version = re.search(r'^version\s*=\s*"([^"]+)"', project_file.read_text(encoding="utf-8"), re.M)
    if not version or version[1] != tag[1:]:
        raise ValueError("Update the project version in pyproject.toml to match the tag (without v)")
    return {"image": "ghcr.io/" + repository.lower(), "tag": tag,
            "version": tag[1:], "prerelease": "true" if match[4] else "false"}


def compose_text(image):
    # JSON-quoted strings are YAML scalars too. No environment interpolation.
    return f'''name: sub2api-flow

services:
  flow:
    image: {json.dumps(image)}
    ports:
      - "127.0.0.1:8787:8787"
    volumes:
      - ./data/web:/app/data
    restart: unless-stopped
    init: true
    stop_grace_period: 2m
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
'''


def write_assets(directory, info, digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("A valid published image digest is required")
    directory.mkdir(parents=True, exist_ok=True)
    # Pin the exact multi-platform manifest, even if somebody later moves the tag.
    image = info["image"] + "@" + digest
    (directory / "docker-compose.yml").write_text(compose_text(image), encoding="utf-8")
    (directory / "image.json").write_text(json.dumps({**info, "digest": digest, "pinned_image": image}, indent=2) + "\n", encoding="utf-8")
    (directory / "DEPLOY.md").write_text(f'''# Flow {info["tag"]}

将本文件和 `docker-compose.yml` 放入空目录，执行：

```sh
docker compose -f docker-compose.yml up -d
```

打开 http://127.0.0.1:8787，首次设置工作台密码，再通过 Web 保存站点账号与同步规则。
所有配置写入 `./data/web/config.json`，密码和密钥加密；无需 `.env` 或业务环境变量。
日志、登录、任务、额度执行记录和上游 Key 映射也保存在 `./data/web`，请完整备份该目录。

当前镜像：`{info["image"]}:{info["tag"]}`，部署文件已固定到 `{digest}`。
支持 linux/amd64 和 linux/arm64。

局域网访问：将 yml 的 `127.0.0.1:8787:8787` 改为 `8787:8787`。
检查状态：`docker compose -f docker-compose.yml ps`。
查看日志：`docker compose -f docker-compose.yml logs -f`。
升级：用新 Release 的部署文件替换原文件，在原目录重新执行启动命令，保留原数据目录。
如果 GHCR 包为私有，请先 `docker login ghcr.io`；公开部署需在 GitHub Packages 将包设为 Public。
''', encoding="utf-8")
    (directory / "release-notes.md").write_text(f'''镜像：`{info["image"]}:{info["tag"]}`（linux/amd64、linux/arm64）。

下载 `docker-compose.yml` 后执行 `docker compose -f docker-compose.yml up -d`，访问 http://127.0.0.1:8787。
通过 Web 配置，配置持久化到 `./data/web/config.json`，无需填写环境变量。
部署文件固定镜像摘要，完整说明见 `DEPLOY.md`。升级时保留整个 `data/web` 目录。
''', encoding="utf-8")
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name not in {"SHA256SUMS", "release-notes.md"})
    (directory / "SHA256SUMS").write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n" for path in files), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--digest")
    args = parser.parse_args()
    try:
        info = release_info(args.repository, args.tag)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.writelines(key + "=" + value + "\n" for key, value in info.items())
        if args.output:
            write_assets(args.output, info, args.digest or "")
    except ValueError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
