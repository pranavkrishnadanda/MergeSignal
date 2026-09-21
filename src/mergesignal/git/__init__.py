"""Git access layer.

Everything that shells out to the ``git`` binary lives here. Three rules hold
across the whole subpackage:

1. **Never mutate user state** (NFR-2). No checkouts, no index writes, no ref
   updates in the user's repository. Simulations use ``merge-tree`` or a
   throwaway worktree that is always cleaned up.
2. Every subprocess call goes through :class:`~mergesignal.git.repo.Repo.run`
   so timeouts, encoding and error wrapping are applied uniformly.
3. Failures surface as :class:`~mergesignal.git.repo.GitError`, never as raw
   :class:`subprocess.CalledProcessError`.
"""

from mergesignal.git.repo import GitError, GitTimeoutError, Repo

__all__ = ["GitError", "GitTimeoutError", "Repo"]
