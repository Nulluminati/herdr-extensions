import subprocess
import tempfile
import unittest
from pathlib import Path

from herdboard.identity import (
    IdentityError,
    RepoIdentity,
    find_board,
    new_board_path,
    normalize_remote,
    repo_identity,
)
from herdboard.model import HerdboardError


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


def init_repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    if origin:
        git(path, "remote", "add", "origin", origin)
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


class NormalizeRemoteTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            "git@github.com:acme/api.git": ("github.com", ("acme", "api")),
            "ssh://git@github.com:22/acme/api.git": ("github.com", ("acme", "api")),
            "https://user:tok@github.com/acme/api": ("github.com", ("acme", "api")),
            "https://github.com/acme/api.git/": ("github.com", ("acme", "api")),
            "git://host/a/b": ("host", ("a", "b")),
            "https://GitLab.COM/group/sub/repo.git": (
                "gitlab.com",
                ("group", "sub", "repo"),
            ),
            "https://github.com:8443/acme/api": ("github.com", ("acme", "api")),
            "user:tok@github.com:acme/api.git": ("github.com", ("acme", "api")),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(normalize_remote(url), expected)

    def test_github_names_are_lowercase(self):
        for url in ["git@github.com:Acme/API.git", "https://github.com/ACME/Api"]:
            with self.subTest(url=url):
                self.assertEqual(normalize_remote(url), ("github.com", ("acme", "api")))

    def test_other_hosts_keep_case(self):
        self.assertEqual(normalize_remote("https://gitlab.com/Group/Repo.git"), ("gitlab.com", ("Group", "Repo")))

    def test_invalid(self):
        for url in [
            "",
            "/srv/git/repo.git",
            "../repo",
            "file:///srv/repo",
            "https://github.com",
            "https://github.com/",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(IdentityError):
                    normalize_remote(url)

    def test_error_type(self):
        self.assertTrue(issubclass(IdentityError, HerdboardError))
        self.assertEqual(IdentityError.code, "identity")


class RepoIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_with_origin_and_worktree(self):
        repo = init_repo(self.root / "api", "git@github.com:acme/api.git")
        wt = self.root / "api-wt"
        git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        main = repo_identity(repo)
        self.assertEqual(
            main,
            RepoIdentity("github.com/acme/api", "github.com", ("acme", "api")),
        )
        self.assertEqual(main.board_stem, "acme-api")
        self.assertEqual(repo_identity(wt), main)

    def test_ssh_and_https_match(self):
        a = init_repo(self.root / "a", "git@github.com:acme/api.git")
        b = init_repo(self.root / "b", "https://github.com/acme/api")
        self.assertEqual(repo_identity(a), repo_identity(b))

    def test_no_origin_and_worktree(self):
        repo = init_repo(self.root / "solo")
        wt = self.root / "solo-wt"
        git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        expected = RepoIdentity(str(repo), None, ("solo",))
        self.assertEqual(repo_identity(repo), expected)
        self.assertEqual(repo_identity(wt), expected)
        self.assertEqual(expected.board_stem, "solo")

    def test_subdirectory(self):
        repo = init_repo(self.root / "solo")
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(repo_identity(sub).identity, str(repo))

    def test_bare_with_worktree(self):
        source = init_repo(self.root / "src")
        bare = self.root / "proj.git"
        git(self.root, "clone", "-q", "--bare", str(source), str(bare))
        git(bare, "remote", "remove", "origin")
        wt = self.root / "proj-wt"
        git(bare, "worktree", "add", "-q", str(wt), "HEAD", "--detach")
        expected = RepoIdentity(str(bare), None, ("proj",))
        self.assertEqual(repo_identity(wt), expected)
        self.assertEqual(repo_identity(bare), expected)

    def test_not_a_repository(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(repo_identity(plain))

    def test_local_path_origin_uses_the_main_checkout(self):
        repo = init_repo(self.root / "r", "/srv/git/r.git")
        ident = repo_identity(repo)
        self.assertEqual(ident.identity, str(repo.resolve()))
        self.assertIsNone(ident.host)
        self.assertEqual(ident.board_stem, "r")


class BoardLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ident = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))

    def board(self, name: str, repo: str) -> Path:
        path = self.dir / name
        path.write_text(f"---\nkanban-plugin: board\nrepo: {repo}\n---\n\n## Todo\n")
        return path

    def test_match(self):
        self.board("other.md", "github.com/acme/web")
        target = self.board("acme-api.md", "github.com/acme/api")
        self.assertEqual(find_board(self.dir, self.ident), target)

    def test_github_match_ignores_case(self):
        target = self.board("acme-api.md", "github.com/Acme/API")
        self.assertEqual(find_board(self.dir, self.ident), target)

    def test_other_host_match_keeps_case(self):
        self.board("g.md", "gitlab.com/Group/Repo")
        ident = RepoIdentity("gitlab.com/group/repo", "gitlab.com", ("group", "repo"))
        self.assertIsNone(find_board(self.dir, ident))

    def test_no_match(self):
        self.board("other.md", "github.com/acme/web")
        self.assertIsNone(find_board(self.dir, self.ident))

    def test_missing_directory(self):
        self.assertIsNone(find_board(self.dir / "nope", self.ident))

    def test_ignores_file_without_frontmatter(self):
        (self.dir / "notes.md").write_text("repo: github.com/acme/api\n")
        (self.dir / "unclosed.md").write_text("---\nrepo: github.com/acme/api\n")
        self.assertIsNone(find_board(self.dir, self.ident))

    def test_two_matches(self):
        self.board("a.md", "github.com/acme/api")
        self.board("b.md", "github.com/acme/api")
        with self.assertRaises(IdentityError) as ctx:
            find_board(self.dir, self.ident)
        self.assertIn("a.md", str(ctx.exception))
        self.assertIn("b.md", str(ctx.exception))

    def test_new_path_free(self):
        self.assertEqual(new_board_path(self.dir, self.ident), self.dir / "acme-api.md")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_new_path_taken_uses_host(self):
        self.board("acme-api.md", "gitlab.com/acme/api")
        self.assertEqual(new_board_path(self.dir, self.ident), self.dir / "github.com-acme-api.md")

    def test_new_path_both_taken(self):
        self.board("acme-api.md", "gitlab.com/acme/api")
        self.board("github.com-acme-api.md", "other/x")
        with self.assertRaises(IdentityError) as ctx:
            new_board_path(self.dir, self.ident)
        self.assertIn("github.com-acme-api.md", str(ctx.exception))

    def test_new_path_taken_without_host(self):
        ident = RepoIdentity("/x/solo", None, ("solo",))
        (self.dir / "solo.md").write_text("x")
        with self.assertRaises(IdentityError) as ctx:
            new_board_path(self.dir, ident)
        self.assertIn("solo.md", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
