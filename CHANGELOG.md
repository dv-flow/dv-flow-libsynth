# Changelog

## Unreleased

### Added
- `synth.xls.Codegen` now also writes the block IR (`xlsBlockIR`) and the Verilog line map (`xlsVerilogLineMap`).
- `CODEGEN_DEFAULTS`, `codegen_argv` and `fix_source_comments` are module-level, so an engine adapter can run the same command the task runs.
