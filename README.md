# dv-flow-libsynth

[dv-flow](https://github.com/dv-flow/dv-flow-mgr) tasks for synthesis-like tools.

| Package | Tasks |
|---|---|
| `synth.xls` | `DslxToIR` (DSLX → XLS IR), `Codegen` (XLS IR → optimized IR → Verilog + module signature + schedule) |

Each task consumes and produces the tool's own file types (`xlsIR`,
`xlsModuleSignature`, ...), so any front end that emits XLS IR can use it. When XLS
rejects a package, the error is reported on the source line that produced the IR
node, using the IR's `pos=` information.

The XLS tools (`opt_main`, `codegen_main`, `ir_converter_main`) are found on `PATH`,
or in `$XLS_BIN`.

```yaml
- name: rtl
  uses: synth.xls.Codegen
  needs: [my-ir]            # a task producing an xlsIR fileset
  with:
    pipeline_stages: 2
```
