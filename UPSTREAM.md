# Upstream Version Tracking

This project uses [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) as a git submodule in the `app/` directory.

## Current Version

- **Commit**: `c9e62c2ab3b4f2cf145668dc0eb4a41bd7649614`
- **Date**: 2026-03-12
- **Status**: Tested and working

## Update Instructions

To update the upstream submodule to a new version:

```bash
# Enter the submodule directory
cd app

# Fetch latest changes
git fetch origin

# Check out a specific commit (recommended)
git checkout <commit-hash>

# Or checkout latest main (not recommended for stability)
git checkout origin/main

# Go back to project root and commit the submodule update
cd ..
git add app
git commit -m "chore: update upstream douyin-downloader to <commit-hash>"
```

## Clone Instructions

When cloning this repository, initialize the submodule:

```bash
git clone --recursive https://github.com/oxyroid/douyin-downloader.git

# Or if already cloned without --recursive:
git submodule update --init
```
