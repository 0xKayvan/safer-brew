# safer-brew

`brew-safe` is a thin wrapper around Homebrew that refuses to install, upgrade or
reinstall a formula or cask whose upstream definition changed in the last N days
(default: 5).

Most malicious formula changes are caught and reverted within a few days. By refusing
packages that were modified very recently, you let the community vetting window do its
work, without pinning your whole tap to an old snapshot.

```console
$ brew-safe install ripgrep some-tool
→ checking that each package has been stable for ≥ 5 days (up to 8 at once)…

  ripgrep                         ✅ stable  41 days       (2026-09-19T08:12:44Z)
  some-tool                       ❌ modified  2 days ago  (2026-10-28T17:03:10Z)

🛑 updated upstream within the last 5 days:
   - some-tool
     review: https://github.com/Homebrew/homebrew-core/commit/…

Refusing to install. Options:
  • wait until the package has aged past 5 days
  • open the review link above and read the change yourself
  • re-run with --only-stable to proceed with just the packages that passed
  • re-run with --force if you have vetted it yourself
```

## How it works

1. For each package, `brew info --json=v2` gives its tap and source path
   (e.g. `homebrew/core` + `Formula/r/ripgrep.rb`).
2. The GitHub API returns the last commit that touched that path. It uses `gh api` when
   it is available and authenticated (5000 requests/hour), and falls back to
   unauthenticated `curl` otherwise (60 requests/hour).
3. If any package was modified within the threshold, brew is not run.

No local clone of `homebrew-core` is needed. Homebrew installs from its JSON API by
default and no longer checks out the core tap.

Only `install`, `upgrade` and `reinstall` are age-checked. Every other subcommand
(`search`, `list`, `tap`, `services`, …) is passed to brew unchanged, flags included.

### Fails closed

If a package cannot be verified, it is refused. The tool reports that case separately
from "too fresh" so you can tell which one applies. Causes include:

- the package lives in a third-party tap (only `homebrew/core` and `homebrew/cask` are
  checked)
- `brew info` failed or returned no source metadata (a typo, or Homebrew is offline)
- the GitHub API is unreachable or rate-limited

## Requirements

- **macOS**. Date parsing uses BSD `date -j`, so Linuxbrew is not supported.
- **Homebrew**
- **bash 4 or newer**. The script uses `mapfile`, and macOS ships bash 3.2 in `/bin/bash`.
  The script runs `#!/usr/bin/env bash`, so Homebrew's bash must come first on your
  `PATH`.
- **jq** and **curl**
- **gh** (optional, recommended), logged in, for the higher API rate limit. Without it,
  a single `brew upgrade` with many outdated packages can use up the 60 requests/hour
  unauthenticated limit.

## Setup

### 1. Install the prerequisites

Do this before you enable the wrapper. Once `brew` is wrapped, these packages would be
age-checked too.

```sh
brew install bash jq gh
gh auth login            # optional, for the 5000 req/hr rate limit
```

Check that the right bash is picked up:

```sh
bash --version           # should report 4.x or 5.x, not 3.2
```

### 2. Clone the repository and put `brew-safe` on your PATH

```sh
git clone https://github.com/0xKayvan/safer-brew.git ~/Developer/safer-brew
mkdir -p ~/.local/bin
ln -s ~/Developer/safer-brew/bin/brew-safe ~/.local/bin/brew-safe
```

Make sure `~/.local/bin` is on your `PATH` (e.g. in `~/.zshrc`):

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Check it:

```sh
brew-safe --version      # not age-checked, so it is passed straight to brew
```

### 3. (Optional) Replace `brew` with `brew-safe`

To make every `brew install` / `upgrade` / `reinstall` go through the age check, wrap
`brew` in a shell function. `brew-safe` itself calls the real `brew` binary on `PATH`,
so the function does not recurse.

**zsh / bash** (`~/.zshrc` or `~/.bashrc`):

```sh
if command -v brew-safe >/dev/null 2>&1; then
  brew() {
    command brew-safe "$@"
  }
fi
```

**fish** (`~/.config/fish/functions/brew.fish`):

```fish
function brew --wraps brew
    brew-safe $argv
end
```

Open a new shell and run `type brew`. It should say `brew` is a function.

When you need the unwrapped binary, `command brew …` bypasses the function.

## Usage

```
brew-safe [-d DAYS] [-j JOBS] [--force | --only-stable] <brew args...>
```

| Flag | Meaning |
| --- | --- |
| `-d`, `--days DAYS` | Minimum age in days (default `5`). `-d 0` disables the threshold for one run. Put `-d` before the subcommand; after it, `-d` is brew's (see below). |
| `-j`, `--jobs JOBS` | How many packages to check in parallel, 1–64 (default `8`). `-j 1` checks them one at a time. |
| `--force` | Run the checks and print the report, then install anyway. |
| `--only-stable` | Run brew on only the packages that passed and skip the rest. Exits `0` if none passed. |
| `--` | Pass everything after it to brew verbatim. |

`--force` and `--only-stable` are mutually exclusive.

These flags can go before or after the subcommand, except `-d`, which only works
before it:

```sh
brew-safe install ripgrep                 # check, then install
brew-safe install --cask firefox          # casks work the same way
brew-safe -d 7 upgrade                    # stricter threshold; expands to every outdated formula
brew-safe upgrade --cask                  # every outdated cask
brew-safe --only-stable upgrade           # upgrade what passes, skip the fresh ones
brew-safe --force install some-tool       # after reading the review link yourself
brew-safe -j 4 upgrade                    # at most 4 checks at once
```

Checks run concurrently, but output stays in argument order and brew runs only once,
after all checks have finished.

### Flags that brew also uses

brew's `install`, `upgrade` and `reinstall` have their own `-d` (`--debug`). brew-safe
reads `-d` as the days threshold only **before** the subcommand. After the subcommand,
`-d` is passed to brew. `--days` works on either side.

```sh
brew-safe -d 0 install ripgrep      # days threshold of 0
brew-safe install -d ripgrep        # brew install --debug, with the default 5-day check
brew-safe install --days 0 ripgrep  # same as the first line
```

`--force` is treated as brew-safe's own flag only for `install`, `upgrade` and
`reinstall`. For any other subcommand it is forwarded to brew, so
`brew-safe tap homebrew/core --force` reaches brew as `brew tap homebrew/core --force`.

To pass `--force` to brew itself on an age-checked subcommand, put it after `--`:

```sh
brew-safe install ripgrep -- --force
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success, or `--only-stable` found nothing safe to install |
| `1` | One or more packages are too fresh or could not be verified |
| `2` | Usage error |

When brew runs, its own exit code is returned.

## Tests

The tests run offline. They put mock `brew`, `gh`, `curl` and `date` executables on
`PATH`, so no network or real Homebrew is touched. They need Python 3 and `jq`.

```sh
python3 -m unittest discover -s tests -v
```

## Uninstall

```sh
rm ~/.local/bin/brew-safe
```

Then remove the `brew` function from your shell config, if you added it.

## License

[GNU Affero General Public License v3.0](LICENSE)
