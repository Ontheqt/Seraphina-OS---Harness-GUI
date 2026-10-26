# Start here

**Seraphina OS Harness 0.2.0** (26 October 2026). Work in progress.

## What this is

A harness for one Ubuntu server and its owner, who is not an expert. AI agents
do the expert work: they write and review code, watch the server and keep
track of what they promised. Every change has a way back. You run it all from
a web page on the server, the GUI.

It is Python 3.10 to 3.14, standard library only. It installs nothing with
pip and needs no containers.

## What works, and what does not yet

The software factory, model routing, the Housekeeping Tracker, chat, personas
and the GUI work, and are tested here against mock servers. Nothing in this
release has run on a real server yet. Disk protection (SnapRAID) and GPU
custody are off until you turn them on.

## Where to read next

| You want | Read |
| --- | --- |
| What changed since the 21 October preview, and what is not checked yet | [RELEASE_NOTES.md](RELEASE_NOTES.md) |
| What it does, and how to install it | [README.md](README.md): the status box, then Quick start |
| To review the code | [REVIEW.md](REVIEW.md): the test commands and what to read first |
| The exact rules the code follows | [DESIGN.md](DESIGN.md) |
| The plans for later | [docs/seraphina/](docs/seraphina/) and [docs/model-factory/](docs/model-factory/MODEL_FACTORY.md) |

## Check it in one command

```
python3 -m unittest discover -s tests -t .
```

It takes about 8 minutes. Every test should pass, and some are skipped
because they need a real server or a lab.

The owner's private notes and the server catalog are not in this release.
