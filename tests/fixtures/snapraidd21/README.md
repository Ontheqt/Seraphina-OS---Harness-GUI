# Recorded SnapRAID Daemon 2.1 exchanges

Requests and answers of SnapRAID Daemon 2.1 (commit `675e46eb`) running SnapRAID 15.1 (commit `4054832c`), recorded
on 2026-10-25 in two scratch labs (docs/seraphina/BACKUPS_SNAPRAID.md, Appendix C):

- `lab/<phase>/`: daemon lab C, real 15.1 on partitioned loop images, one folder per phase of the run
  (`04-after-maintenance`, `07-after-threshold-abort`, `18-after-restart`, ...);
- `api/`: daemon lab A, a fake engine, for the HTTP surface (401, 429, 409, 500, 204, a repeated stop, ...);
- `notify/`: the `notify_result` argument lines of each report of lab C, and one report's text.

Each exchange is `{method, path, body, status, headers, json | text}`. Sanitized for publication: the labs' scratch
paths are removed (paths read `/mnt/d1/`, `/etc/snapraid.conf`, `/var/log/...`), the notify command is
`/usr/local/bin/notify-example`, the host name is `<host>`, only the `Content-Type`, `Retry-After`,
`WWW-Authenticate` and `Connection` headers are kept, no request carries a credential, and the routes Seraphina never
calls (`/system`, the deprecated `/v1/disks` and `/v1/config`, `PATCH`) are left out. The file names in change lists
are the labs' own synthetic names. tests/mock_snapraidd.py seeds its state from `lab/04-after-maintenance/`.
