# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2026-04-03

### Added

- Initial release: extracted from ufo-widgets-gtk4 and TDKit
- `constants` module: shared PS hint constants and outline hash computation
- `extraction` module: binary font (OTF/TTF/WOFF/WOFF2) to UFO conversion with CFF hint extraction and feature cleanup
- `ps_hints` module: PS hint parsing, optimization, layer conversion, and structural validation
- `compilation` module: UFO to OTF compilation with PS hint preservation (preserve-optimized mode)
