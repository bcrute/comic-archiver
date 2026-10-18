Gitea runs the workflows in this folder, and only those. It has none: development checks run on the owner's
own CI hosts, and `.github/workflows/release.yml` verifies and publishes their staged downloads on GitHub. While this folder exists, Gitea
does not pick up the GitHub workflow.
