#!/usr/bin/env python3
#
# yosys -- Yosys Open SYnthesis Suite
#
# Copyright (C) 2022  Jannis Harder <jix@yosyshq.com> <me@jix.one>
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#

import os, sys, itertools, re
##yosys-sys-path##
import json
import click

from ywio import ReadWitness, WriteWitness, WitnessSig, WitnessSigMap, WitnessValues, coalesce_signals

@click.group()
def cli():
    pass


@cli.command(help="""
Display a Yosys witness trace in a human readable format.
""")
@click.argument("input", type=click.File("r"))
def display(input):
    click.echo(f"Reading Yosys witness trace {input.name!r}...")
    inyw = ReadWitness(input)

    def output():

        yield click.style("*** RTLIL bit-order below may differ from source level declarations ***", fg="red")
        if inyw.clocks:
            yield click.style("=== Clock Signals ===", fg="blue")
            for clock in inyw.clocks:
                yield f"  {clock['edge']} {WitnessSig(clock['path'], clock['offset']).pretty()}"

        for t, values in inyw.steps():
            if t:
                yield click.style(f"=== Step {t} ===", fg="blue")
            else:
                yield click.style("=== Initial State ===", fg="blue")

            step_prefix = click.style(f"#{t}", fg="bright_black")

            signals, missing = values.present_signals(inyw.sigmap)

            assert not missing

            for sig in signals:
                display_bits = values[sig].replace("?", click.style("?", fg="bright_black"))
                yield f"  {step_prefix} {sig.pretty()} = {display_bits}"
    click.echo_via_pager([line + "\n" for line in output()])


@cli.command(help="""
Display statistics of a Yosys witness trace.
""")
@click.argument("input", type=click.File("r"))
def stats(input):
    click.echo(f"Reading Yosys witness trace {input.name!r}...")
    inyw = ReadWitness(input)

    total = 0

    for t, values in inyw.steps():
        click.echo(f"{t:5}: {len(values.values):8} bits")
        total += len(values.values)

    click.echo(f"total: {total:8} bits")


@cli.command(help="""
Transform a Yosys witness trace.

Currently no transformations are implemented, so it is only useful for testing.
""")
@click.argument("input", type=click.File("r"))
@click.argument("output", type=click.File("w"))
def yw2yw(input, output):
    click.echo(f"Copying yosys witness trace from {input.name!r} to {output.name!r}...")
    inyw = ReadWitness(input)
    outyw = WriteWitness(output, "yosys-witness yw2yw")

    for clock in inyw.clocks:
        outyw.add_clock(clock["path"], clock["offset"], clock["edge"])

    for sig in inyw.signals:
        outyw.add_sig(sig.path, sig.offset, sig.width, sig.init_only)

    for t, values in inyw.steps():
        outyw.step(values)

    outyw.end_trace()

    click.echo(f"Copied {outyw.t + 1} time steps.")


class AigerMap:
    def __init__(self, mapfile):
        data = json.load(mapfile)

        version = data.get("version") if isinstance(data, dict) else {}
        if version != "Yosys Witness Aiger map":
            raise click.ClickException(f"{mapfile.name}: unexpected file format version {version!r}")

        self.latch_count = data["latch_count"]
        self.input_count = data["input_count"]

        self.clocks = data["clocks"]

        self.sigmap = WitnessSigMap()
        self.init_inputs = set(init["input"] for init in data["inits"])

        for bit in data["inputs"] + data["seqs"] + data["inits"]:
            self.sigmap.add_bit((tuple(bit["path"]), bit["offset"]), bit["input"])



@cli.command(help="""
Convert an AIGER witness trace into a Yosys witness trace.

This requires a Yosys witness AIGER map file as generated by 'write_aiger -ywmap'.
""")
@click.argument("input", type=click.File("r"))
@click.argument("mapfile", type=click.File("r"))
@click.argument("output", type=click.File("w"))
def aiw2yw(input, mapfile, output):
    input_name = input.name
    click.echo(f"Converting AIGER witness trace {input_name!r} to Yosys witness trace {output.name!r}...")
    click.echo(f"Using Yosys witness AIGER map file {mapfile.name!r}")
    aiger_map = AigerMap(mapfile)

    header_lines = list(itertools.islice(input, 0, 2))

    if len(header_lines) == 2 and header_lines[1][0] in ".bcjf":
        status = header_lines[0].strip()
        if status == "0":
            raise click.ClickException(f"{input_name}: file contains no trace, the AIGER status is unsat")
        elif status == "2":
            raise click.ClickException(f"{input_name}: file contains no trace, the AIGER status is sat")
        elif status != "1":
            raise click.ClickException(f"{input_name}: unexpected data in AIGER witness file")
    else:
        input = itertools.chain(header_lines, input)

    ffline = next(input, None)
    if ffline is None:
        raise click.ClickException(f"{input_name}: unexpected end of file")
    ffline = ffline.strip()
    if not re.match(r'[01x]*$', ffline):
        raise click.ClickException(f"{input_name}: unexpected data in AIGER witness file")
    if not re.match(r'[0]*$', ffline):
        raise click.ClickException(f"{input_name}: non-default initial state not supported")

    outyw = WriteWitness(output, 'yosys-witness aiw2yw')

    for clock in aiger_map.clocks:
        outyw.add_clock(clock["path"], clock["offset"], clock["edge"])

    for (path, offset), id in aiger_map.sigmap.bit_to_id.items():
        outyw.add_sig(path, offset, init_only=id in aiger_map.init_inputs)

    missing = set()

    while True:
        inline = next(input, None)
        if inline is None:
            click.echo(f"Warning: {input_name}: file may be incomplete")
            break
        inline = inline.strip()
        if inline in [".", "# DONE"]:
            break
        if inline.startswith("#"):
            continue

        if not re.match(r'[01x]*$', ffline):
            raise click.ClickException(f"{input_name}: unexpected data in AIGER witness file")

        if len(inline) != aiger_map.input_count:
            raise click.ClickException(
                    f"{input_name}: {mapfile.name}: number of inputs does not match, "
                    f"{len(inline)} in witness, {aiger_map.input_count} in map file")

        values = WitnessValues()
        for i, v in enumerate(inline):
            if v == "x" or outyw.t > 0 and i in aiger_map.init_inputs:
                continue

            try:
                bit = aiger_map.sigmap.id_to_bit[i]
            except IndexError:
                bit = None
            if bit is None:
                missing.insert(i)

            values[bit] = v

        outyw.step(values)

    outyw.end_trace()

    if missing:
        click.echo("The following AIGER inputs belong to unknown signals:")
        click.echo("  " + " ".join(str(id) for id in sorted(missing)))

    click.echo(f"Converted {outyw.t} time steps.")

@cli.command(help="""
Convert a Yosys witness trace into an AIGER witness trace.

This requires a Yosys witness AIGER map file as generated by 'write_aiger -ywmap'.
""")
@click.argument("input", type=click.File("r"))
@click.argument("mapfile", type=click.File("r"))
@click.argument("output", type=click.File("w"))
def yw2aiw(input, mapfile, output):
    click.echo(f"Converting Yosys witness trace {input.name!r} to AIGER witness trace {output.name!r}...")
    click.echo(f"Using Yosys witness AIGER map file {mapfile.name!r}")
    aiger_map = AigerMap(mapfile)
    inyw = ReadWitness(input)

    print("1", file=output)
    print("b0", file=output)
    # TODO the b0 status isn't really accurate, but we don't have any better info here
    print("0" * aiger_map.latch_count, file=output)

    all_missing = set()

    for t, step in inyw.steps():
        bits, missing = step.pack_present(aiger_map.sigmap)
        bits = bits[::-1].replace('?', 'x')
        all_missing.update(missing)
        bits += 'x' * (aiger_map.input_count - len(bits))
        print(bits, file=output)

    print(".", file=output)

    if all_missing:
        click.echo("The following signals are missing in the AIGER map file and will be dropped:")
        for sig in coalesce_signals(WitnessSig(*bit) for bit in all_missing):
            click.echo("  " + sig.pretty())


    click.echo(f"Converted {len(inyw)} time steps.")

class BtorMap:
    def __init__(self, mapfile):
        self.data = data = json.load(mapfile)

        version = data.get("version") if isinstance(data, dict) else {}
        if version != "Yosys Witness BTOR map":
            raise click.ClickException(f"{mapfile.name}: unexpected file format version {version!r}")

        self.sigmap = WitnessSigMap()

        for mode in ("states", "inputs"):
            for btor_signal_def in data[mode]:
                if btor_signal_def is None:
                    continue
                if isinstance(btor_signal_def, dict):
                    btor_signal_def["path"] = tuple(btor_signal_def["path"])
                else:
                    for chunk in btor_signal_def:
                        chunk["path"] = tuple(chunk["path"])


@cli.command(help="""
Convert a BTOR witness trace into a Yosys witness trace.

This requires a Yosys witness BTOR map file as generated by 'write_btor -ywmap'.
""")
@click.argument("input", type=click.File("r"))
@click.argument("mapfile", type=click.File("r"))
@click.argument("output", type=click.File("w"))
def wit2yw(input, mapfile, output):
    input_name = input.name
    click.echo(f"Converting BTOR witness trace {input_name!r} to Yosys witness trace {output.name!r}...")
    click.echo(f"Using Yosys witness BTOR map file {mapfile.name!r}")
    btor_map = BtorMap(mapfile)

    input = filter(None, (line.split(';', 1)[0].strip() for line in input))

    sat = next(input, None)
    if sat != "sat":
        raise click.ClickException(f"{input_name}: not a BTOR witness file")

    props = next(input, None)

    t = -1

    line = next(input, None)

    frames = []
    bits = {}

    while line and not line.startswith('.'):
        current_t = int(line[1:].strip())
        if line[0] == '#':
            mode = "states"
        elif line[0] == '@':
            mode = "inputs"
        else:
            raise click.ClickException(f"{input_name}: unexpected data in BTOR witness file")

        if current_t > t:
            t = current_t
            values = WitnessValues()
            frames.append(values)

        line = next(input, None)
        while line and line[0] not in "#@.":
            if current_t > 0 and mode == "states":
                line = next(input, None)
                continue
            tokens = line.split()
            line = next(input, None)

            btor_sig = btor_map.data[mode][int(tokens[0])]

            if btor_sig is None:
                continue

            if isinstance(btor_sig, dict):
                addr = tokens[1]
                if not addr.startswith('[') or not addr.endswith(']'):
                    raise click.ClickException(f"{input_name}: expected address in BTOR witness file")
                addr = int(addr[1:-1], 2)

                if addr < 0 or addr >= btor_sig["size"]:
                    raise click.ClickException(f"{input_name}: out of bounds address in BTOR witness file")

                btor_sig = [{
                    "path": (*btor_sig["path"], f"\\[{addr}]"),
                    "width": btor_sig["width"],
                    "offset": 0,
                }]

                signal_value = iter(reversed(tokens[2]))
            else:
                signal_value = iter(reversed(tokens[1]))

            for chunk in btor_sig:
                offset = chunk["offset"]
                path = chunk["path"]
                for i in range(offset, offset + chunk["width"]):
                    key = (path, i)
                    bits[key] = mode == "inputs"
                    values[key] = next(signal_value)

            if next(signal_value, None) is not None:
                raise click.ClickException(f"{input_name}: excess bits in BTOR witness file")


    if line is None:
        raise click.ClickException(f"{input_name}: unexpected end of BTOR witness file")
    if line.strip() != '.':
        raise click.ClickException(f"{input_name}: unexpected data in BTOR witness file")
    if next(input, None) is not None:
        raise click.ClickException(f"{input_name}: unexpected trailing data in BTOR witness file")

    outyw = WriteWitness(output, 'yosys-witness wit2yw')

    outyw.signals = coalesce_signals((), bits)
    for clock in btor_map.data["clocks"]:
        outyw.add_clock(clock["path"], clock["offset"], clock["edge"])

    for values in frames:
        outyw.step(values)

    outyw.end_trace()
    click.echo(f"Converted {outyw.t} time steps.")

if __name__ == "__main__":
    cli()
