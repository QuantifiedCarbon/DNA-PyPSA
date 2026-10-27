# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project scaffold
- `expand` mode: capacity expansion pre-solve followed by single dispatch
- `iterate` mode: capacity expansion pre-solve followed by iterative variability and capacity adjustment
- JSON scenario config with support for scalar, inline array, and CSV file-reference timeseries values
- CSV topology input format (structural columns only)
- Results output to both CSV folder and netCDF
