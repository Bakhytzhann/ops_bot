import hashlib
import re
from pathlib import Path

from .process import run


class GitManager:
    def __init__(self, root, redact):
        self.root, self.redact = root, redact

    async def git(self, *args):
        code, output = await run(["git", *args], self.root)
        if code:
            raise RuntimeError(self.redact(output))
        if output.startswith("[OUTPUT TRUNCATED]"):
            raise ValueError("Git output exceeded safety limit; operation aborted")
        return output.rstrip("\r\n")

    async def clean(self):
        if await self.git("status", "--porcelain"):
            raise ValueError(
                "Working tree is dirty; commit or preserve local changes first"
            )

    async def identity(self):
        return {
            "head": await self.git("rev-parse", "HEAD"),
            "branch": await self.git("branch", "--show-current"),
            "remote": await self.git("remote", "get-url", "origin"),
        }

    async def fingerprint(self):
        identity = await self.identity()
        status = await self.git("status", "--porcelain", "-z")
        digest = hashlib.sha256((str(identity) + status).encode())
        files = (
            await self.git(
                "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            )
        ).split("\0")
        for name in sorted(set(files)):
            if not name:
                continue
            path = self.root / name
            if path.is_symlink() or not path.resolve().is_relative_to(
                self.root.resolve()
            ):
                raise ValueError("Symlink or external path rejected")
            digest.update(name.encode())
            if path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    async def review(self):
        return (
            await self.git("status", "--short")
            + "\n"
            + await self.git("diff", "--stat", "HEAD")
        )

    async def publication_check(self):
        identity = await self.identity()
        if not identity["branch"] or not re.fullmatch(
            r"(?:https://github.com/|git@github.com:)Bakhytzhann/ops_bot(?:\.git)?",
            identity["remote"],
        ):
            raise ValueError("Unexpected branch or origin repository")
        files = (
            await self.git(
                "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            )
        ).split("\0")
        for name in files:
            if not name:
                continue
            p = Path(name)
            if (
                p.name.startswith(".env") and p.name != ".env.example"
            ) or p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".key", ".pem"}:
                raise ValueError("Sensitive file cannot be published: " + name)
            path = self.root / p
            if path.is_file():
                content = path.read_bytes()
                text = content.decode("utf-8", errors="replace")
                if self.redact.contains_secret(text):
                    raise ValueError("Possible secret in " + name)
        return identity

    async def push(self):
        identity = await self.publication_check()
        # Check remote-only commits as well as current files.
        await self.git("fetch", "origin")
        upstream = "refs/remotes/origin/" + identity["branch"]
        remote_branch = await self.git("for-each-ref", "--format=%(refname)", upstream)
        if upstream in remote_branch.splitlines():
            await self.git("merge-base", "--is-ancestor", upstream, "HEAD")
            outgoing = [upstream + "..HEAD"]
        else:
            # A new branch has no upstream yet. Inspect every commit that is
            # not already reachable from origin before publishing it.
            outgoing = ["HEAD", "--not", "--remotes=origin"]
        history = await self.git("log", "-p", *outgoing)
        if self.redact.contains_secret(history):
            raise ValueError("Possible secret in outgoing history")
        names = await self.git("log", "--format=", "--name-only", *outgoing)
        for name in names.splitlines():
            p = Path(name)
            if (
                p.name.startswith(".env") and p.name != ".env.example"
            ) or p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".key", ".pem"}:
                raise ValueError("Sensitive file in outgoing history: " + name)
        await self.git("add", "--all")
        changed = (await self.git("diff", "--cached", "--name-only")).splitlines()
        if changed:
            message = "Update " + ", ".join(changed[:3])
            if len(changed) > 3:
                message += f" and {len(changed) - 3} other files"
            await self.git("commit", "-m", message[:180])
        return await self.git(
            "push", "--set-upstream", "origin", "HEAD:refs/heads/" + identity["branch"]
        )

    async def pull(self):
        await self.clean()
        identity = await self.publication_check()
        await self.git("fetch", "origin")
        return await self.git("merge", "--ff-only", "origin/" + identity["branch"])
