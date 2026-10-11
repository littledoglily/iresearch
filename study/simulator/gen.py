#!/usr/bin/env python3
"""Regenerate the example sections of field_write_memory_flush_pipeline.md into the current directory:
   sec28.md (2.8), sec32_examples.md (3.2.5-3.2.7), sec33_examples.md (3.3.3-3.3.4), sec35.md (3.5).
Pure python3, no dependencies.  python3 columns_demo.py prints the 2.10 column demos."""
from render import *
import render3

w, cols, steps = run()
M = flush_model(w, cols)
F, T, blocks, fm = build(w, cols, M)
open('sec28.md', 'w').write(render_28(w, cols, steps))
s32, s33 = render3.gen(w, cols, M, F, blocks, fm)
open('sec32_examples.md', 'w').write(s32)
open('sec33_examples.md', 'w').write(s33)
open('sec35.md', 'w').write(render_35(w, cols, M, F, T, blocks, fm))
print('wrote sec28.md sec32_examples.md sec33_examples.md sec35.md')
