<h1 align="center">
  phoenix-software
  <br>
</h1>

<p align="center">
  Software for <b>Phoenix</b>, the radio communications system built by <a href="https://github.com/Domer-Rover">Domer Rover</a>.
  <br />
  Originally developed for the Phoenix rover, this repo now houses the custom radio comms stack shared across future robots.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-none-lightgrey" alt="License">
  <img src="https://img.shields.io/badge/Software%20Lead-N%2FA-lightgrey" alt="Software Lead">
  <img src="https://img.shields.io/badge/CI-Archived-inactive" alt="CI Status">
</p>

<p align="center">
  <a href="https://github.com/Domer-Rover">Domer Rover</a>
  ·
  <a href="https://github.com/Domer-Rover/phoenix-software/issues">Report an Issue</a>
</p>

---

> **⚠️ Archived:** This repo is no longer actively supported. It's kept around as the home for the radio communications system, which future Domer Rover robots build on.

## Overview

Phoenix was Domer Rover's rover platform, and this repository was its ROS 2 software stack. Active development has moved on, but the **custom radio communication system** built here continues to be used and extended for future robots.

## Directory Structure

| Path | Description |
| --- | --- |
| `radio_comms` | Custom radio communication system |
| `ws/src` | Archived ROS 2 packages (2024–2025 Domer Rover, post-SAR) |

## Notes

- **Do not install `libv4l-dev`** — installing this apt package on Jetson at any point will break hardware encoding/decoding support.

## About Domer Rover

[Domer Rover](https://github.com/Domer-Rover) is the University of Notre Dame's rover team, competing at the University Rover Challenge (URC).

## License

No license file is currently included in this repository.
