# Known Issues

- Phase 1 does not yet include video-processing dependencies or features.
- Rust and Windows build tools must be available before the Tauri shell can build.
- The application icon is not yet included.
- The backend is not yet launched automatically by Tauri.
- No project migrations framework is configured yet; the foundation currently uses schema creation.
- **Tauri build environment block:** Windows Smart App Control/Code Integrity is enforcing an Enterprise signing level (Policy ID `0283ac0f-fff1-49ae-ada1-8a933130cad6`). Cargo 1.100.0 reaches `serde_core v1.0.229`, then Windows blocks execution of its generated unsigned executable:
  `src-tauri\target\debug\build\serde_core\0047b5762ea76b51\out\build_script_build.exe`
- The same policy also blocked compiled SQLAlchemy/Python native extensions; the source/rebuilt SQLAlchemy 2.0.36 and both backend tests now run successfully. The block is not evidence of a malicious dependency.
- Effective AppLocker policy is empty; this is not an AppLocker rule or a dependency-location conflict. The affected Rust executables are unsigned build artifacts created in the normal Cargo target directory. `cargo.exe` and `rustc.exe` are also unsigned in this rustup environment, while Node.js is signed.
- Safe resolution: build in an authorized CI/clean development environment that permits dynamically generated Rust build scripts, then sign and deliver the final Tauri installer normally. For local development, use the Windows Security policy's supported trust or developer workflow for unsigned build tools through the organization or device owner. Do not disable Smart App Control or Application Control.
