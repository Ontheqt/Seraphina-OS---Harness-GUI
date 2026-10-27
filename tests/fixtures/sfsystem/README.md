Fixtures for bin/sf-system and swfactory/yourserver.py (docs/seraphina/YOUR_SERVER.md §12.2). Every id, name, address
and path is a placeholder: 192.0.2.x addresses, example.ts.net, home.example, apps called photos, media, notes. The
Docker files follow Docker 29's checkpoint shape (config.v2.json, hostconfig.json) as the design's lab recorded it;
they were written for these tests, not copied from a real server. tests/sfsystem_rig.py builds the rest of the
machine (containers, Tailscale state files with planted key material, nginx configs with planted secrets, the tools'
outputs) in a temporary tree.

From P1b and P1c: `smart/` (smartctl -j answers of a healthy NVMe and SATA disk, a failing one, one asleep with exit
3, and the "Permission denied" exit 2 a user who is not root gets), `checks/` (needrestart -b, journalctl
--disk-usage with and without files, du -B1, apt-get update), and `access/` (sudo -l and -ll texts, sshd -T,
authorized_keys files whose public keys were made for these tests with ssh-keygen, their fingerprints as ssh-keygen
-lf printed them, systemctl's service list and show output with root services that run a script in a home folder,
find's setuid list, getcap, passwd -S, cvtsudoers JSON, ss -x). The Docker API's answers are built by the rig from
the same state files (Rig.docker_answer). Every account, unit, path, key comment and log line is made up.
