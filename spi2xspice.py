#!ENV_PATH python3
#
# spi2xspice.py
#
# Simple script to convert a standard-cell SPICE subcircuit (e.g., one created
# by qflow) into a version replacing all the standard cells with XSPICE
# primitives.  This works in conjunction with lib2xspice.py, which converts
# a liberty file for a standard cell set into a set of XSPICE models using
# d_lut, d_genlut, d_dff, and d_dlatch.
#
# Written by Tim Edwards
# efabless, inc. 2017
# May 17, 2017
# Updated Dec. 22, 2018 to accomodate in-lined standard cell subcircuits
# (i.e., not from a .include statement) and corrected duplicate .end statement.
# Updated Jan. 12, 2024 to fix port direction when inputs and outputs are not
# buffered, and a subcircuit output signal is also fed back to other gates in
# the subcircuit.
#
# Updated Nov 2026 by Yacine Sotehi
# - Added accurate instance-specific delay calculations using Liberty lookup tables.
# - Replaced monolithic median plane-fitting with true 2D bilinear interpolation.
# - Implemented topological circuit graph extraction and recursive slew propagation.
# - Replaced static .model generation with per-instance dynamic .model definitions based on exact net loads.
# - Replaced static ADC/DAC bridges with dynamic, power-aware normalized B-sources tied to VDD/VSS.
#
# This script is in the public domain
#--------------------------------------------------------------------------

import re
import sys
import os
import statistics
import bisect


######
## Yacine UPDATES: Including proper timing calculations from liberty file
######
# Defaults per Liberty conventions
TIME_UNIT_SEC = 1e-9      # 1ns
CAP_UNIT_FARAD = 1e-12    # 1pf
computed_slews = {}
net_caps = {}
net_driver = {}
inst_data = {}

## Helpers functions to parse time and capacitance units from liberty files

def parse_value_from_unit(value, unit):
    unit = unit if unit else 'unit'
    scale = {'unit': 1.0, 'm': 1e-3, 'u': 1e-6, 'n': 1e-9,
                 'p': 1e-12, 'f': 1e-15}.get(unit, 1e-9)
    return value * scale

def parse_float_from_unit(s):
    unit = s[-1] if s[-1].isalpha() else ""
    value = float(s[:-1]) if unit else float(s)
    return parse_value_from_unit(value, unit)

def parse_unit(line):
    # matches: time_unit : "1ns";
    m = re.search(r'time_unit\s*:\s*"([0-9.]+)\s*([a-z]+)"', line)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)

    return parse_value_from_unit(val, unit[1] if len(unit) > 1 else "")


def values_to_spice_unit(value):
    if value < 1e-15:
        return '{:.3g}a'.format(value * 1e18)
    elif value < 1e-12:
        return '{:.3g}f'.format(value * 1e15)
    elif value < 1e-9:
        return '{:.3g}p'.format(value * 1e12)
    elif value < 1e-6:
        return '{:.3g}n'.format(value * 1e9)
    elif value < 1e-3:
        return '{:.3g}u'.format(value * 1e6)
    else:
        return '{:.3g}m'.format(value * 1e3)
    

def pick_primary_arcs(cellrec):
    return cellrec.get('timing_arcs', [])
    #return prim

## TO REMOVE: used to write general models with median delay values
def fit_plane(index1, index2, values):
    """
    Fit delay = a + b*slew + c*load to a 2-D table.
    Columns are scaled to O(1) before solving to avoid
    the ill-conditioning that made det3 return negative intercepts.
    """
    m, n = len(index1), len(index2)
    if m == 0 or n == 0 or len(values) != m * n:
        return None

    # Column scaling: bring slew and load into comparable ranges
    s_ref = max(abs(x) for x in index1) or 1.0
    l_ref = max(abs(x) for x in index2) or 1.0
    s_scaled = [x / s_ref for x in index1]
    l_scaled = [x / l_ref for x in index2]

    S = [[0.0]*3 for _ in range(3)]
    T = [0.0]*3
    for i in range(m):
        for j in range(n):
            x = (1.0, s_scaled[i], l_scaled[j])
            y = values[i*n + j]
            for r in range(3):
                T[r] += x[r] * y
                for c in range(3):
                    S[r][c] += x[r] * x[c]

    def det3(M):
        return (M[0][0]*(M[1][1]*M[2][2]-M[1][2]*M[2][1])
               -M[0][1]*(M[1][0]*M[2][2]-M[1][2]*M[2][0])
               +M[0][2]*(M[1][0]*M[2][1]-M[1][1]*M[2][0]))

    D = det3(S)
    if abs(D) < 1e-20:
        return None

    def replace_col(M, col, v):
        M2 = [row[:] for row in M]
        for r in range(3):
            M2[r][col] = v[r]
        return M2

    a = det3(replace_col(S, 0, T)) / D
    b = det3(replace_col(S, 1, T)) / D
    c = det3(replace_col(S, 2, T)) / D

    # Unscale the slopes back to original units
    b /= s_ref
    c /= l_ref

    return (a, b, c)

def cell_intrinsic_delay(cellrec, arc_types=('combinational', 'combinational_rise', 'combinational_fall')):
    rise_list, fall_list = [], []
    for arc in cellrec.get('timing_arcs', []):
        ttype = (arc.get('timing_type') or '').lower()
        if ttype not in arc_types:
            continue
        tables = arc.get('tables', {})
        for key, bucket in (('cell_rise', rise_list), ('cell_fall', fall_list)):
            t = tables.get(key)
            if not t:
                continue
            fit = fit_plane(t.get('index_1') or [], t.get('index_2') or [], t.get('values') or [])
            if fit:
                a = fit[0]
                if a < 0:
                    a = min(t['values'])
                bucket.append(a)
    rise = statistics.median(rise_list) if rise_list else None
    fall = statistics.median(fall_list) if fall_list else None
    return rise, fall


def calculate_net_loads(spilines, celldefs, default_cload):
    net_caps = {}
    xrex = re.compile('^[ \t]*X([^ \t]+)(.*)$', re.IGNORECASE)
    
    # First pass: sum up the input pin capacitances for each net
    for line in spilines:
        xmatch = xrex.match(line)
        if xmatch:
            conns = [item for item in xmatch.group(2).split() if '=' not in item and item != '/']
            pins = conns[0:-1]
            cellname = conns[-1]
            if cellname in celldefs:
                cellrec = celldefs[cellname]
                for pin_idx, pin_name in enumerate(cellrec.get('inputs', [])):
                    # Map the celldef input pin to the netlist connection
                    if pin_name in cellrec.get('spicepins', []):
                        spice_idx = cellrec['spicepins'].index(pin_name)
                        if spice_idx < len(pins):
                            net_name = pins[spice_idx]
                            # Add pin capacitance (fallback to default if missing)
                            pin_cap = cellrec['input_caps'][pin_idx] if pin_idx < len(cellrec['input_caps']) else default_cload
                            net_caps[net_name] = net_caps.get(net_name, 0.0) + pin_cap
    return net_caps


def get_net_slew(net_name, net_driver, inst_data, celldefs, net_caps, computed_slews, default_slew, visited=None):
    if visited is None:
        visited = set()

    # 1. Loop Detection: Break cyclic dependencies (e.g., ring oscillators)
    if net_name in visited:
        return default_slew

    # 2. Return cached result if already computed
    if net_name in computed_slews:
        return computed_slews[net_name]
        
    # If the net has no driver in our cell list, it's a primary input
    if net_name not in net_driver:
        computed_slews[net_name] = default_slew
        return default_slew
        
    # Mark this net as being actively evaluated in the current path
    visited.add(net_name)

    driver_inst = net_driver[net_name]
    driver_data = inst_data[driver_inst]
    cellrec = celldefs[driver_data['cell']]
    
    # Recursively get the worst-case input slew arriving at the driving gate
    in_slews = [get_net_slew(in_net, net_driver, inst_data, celldefs, net_caps, computed_slews, default_slew, visited) 
                for in_net in driver_data['inputs']]
    worst_in_slew = max(in_slews) if in_slews else default_slew
    
    # Get the capacitive load on the driver's output net
    out_load = net_caps.get(net_name, 0.0)
    
    # Look up the transition tables for the driving cell
    arc = pick_primary_arcs(cellrec)[0] 
    rise_trans = arc['tables'].get('rise_transition')
    fall_trans = arc['tables'].get('fall_transition')
    
    if rise_trans and fall_trans:
        r_slew = interpolate_2d(rise_trans['index_1'], rise_trans['index_2'], rise_trans['values'], worst_in_slew, out_load)
        f_slew = interpolate_2d(fall_trans['index_1'], fall_trans['index_2'], fall_trans['values'], worst_in_slew, out_load)
        out_slew = max(r_slew, f_slew)
    else:
        out_slew = default_slew

    # Cache the result and remove from active path
    computed_slews[net_name] = out_slew
    visited.remove(net_name)
    
    return out_slew

def build_circuit_graph(spilines, celldefs):
    net_driver = {}  # net_name -> driving_instance_name
    inst_data = {}   # inst_name -> {'cell': cellname, 'inputs': [], 'outputs': []}
    
    xrex = re.compile('^[ \t]*X([^ \t]+)(.*)$', re.IGNORECASE)
    
    for line in spilines:
        xmatch = xrex.match(line)
        if xmatch:
            instname = xmatch.group(1)
            conns = [c for c in xmatch.group(2).split() if '=' not in c and c != '/']
            cellname = conns[-1]
            pins = conns[:-1]
            
            if cellname in celldefs:
                cellrec = celldefs[cellname]
                inputs, outputs = [], []
                
                # Map SPICE pin order to Liberty input/output directions
                for i, pin_name in enumerate(cellrec.get('spicepins', [])):
                    if i < len(pins):
                        net = pins[i]
                        if pin_name in cellrec.get('outputs', []):
                            outputs.append(net)
                            net_driver[net] = instname  # Record that this instance drives this net
                        else:
                            inputs.append(net)
                            
                inst_data[instname] = {
                    'cell': cellname, 
                    'inputs': inputs, 
                    'outputs': outputs
                }
                
    return net_driver, inst_data

def interpolate_2d(idx1, idx2, values, slew, load):
    # Clamp inputs to table boundaries to prevent extrapolation errors
    val1 = max(idx1[0], min(slew, idx1[-1]))
    val2 = max(idx2[0], min(load, idx2[-1]))

    # Find the lower bounding indices
    i = min(bisect.bisect_right(idx1, val1) - 1, len(idx1) - 2)
    j = min(bisect.bisect_right(idx2, val2) - 1, len(idx2) - 2)
    i = max(0, i)
    j = max(0, j)

    x1, x2 = idx1[i], idx1[i+1]
    y1, y2 = idx2[j], idx2[j+1]

    # Map 2D coordinates to the 1D values array
    def get_v(r, c): return values[r * len(idx2) + c]
    v11, v12 = get_v(i, j), get_v(i, j+1)
    v21, v22 = get_v(i+1, j), get_v(i+1, j+1)

    # Bilinear interpolation
    f_x_y1 = v11 + (val1 - x1) * (v21 - v11) / (x2 - x1)
    f_x_y2 = v12 + (val1 - x1) * (v22 - v12) / (x2 - x1)
    return f_x_y1 + (val2 - y1) * (f_x_y2 - f_x_y1) / (y2 - y1)


def generate_lut_table(cellrec):
    imprex = re.compile('([10\)])[ \t]+([10\(])')
    tabstr = ''
    nout = cellrec['nout']
    nin = cellrec['nin']
    # if len(cellrec['function']) < nout:
    #     # Some outputs have no combinational function (e.g. DFF Q).
    #     # Skip LUT generation for those pins.
    #     nout = len(cellrec['function'])
                    
    for k in range(0, nout):
        # Handle tristate functions
        # If triidx < nin then triidx, tripin, and tripos are all valid
        if 'tristate' in cellrec:
            trirec = cellrec['tristate']
            if trirec[0] == '~':
                tripos = False
                tripin = trirec[1:]
            else:
                tripos = True
                tripin = trirec
            for triidx in range(0, nin):
                if cellrec['inputs'][triidx] == tripin:
                    break
            # Index into binstring is the reverse of the pin order
            triidx = nin - triidx - 1
        else:
            triidx = nin

        pstring = parse_pin(cellrec['function'][k])
        nvals = 2**nin
        for i in range(0, nvals):
            # binstring is the binary count of input bits.  Note that
            # the index into binstring is reverse of the pin order.
            # (e.g., "000", "001", "010", "011", ...)
            binstring = '{0:{fill}{width}b}'.format(i, fill='0', width=nin)

            psubs = pstring
            for j in range(0, nin):
                bchar = cellrec['inputs'][nin - j - 1].strip('"')
                bval = binstring[j]
                psubs = psubs.replace(bchar, bval)

            # Handle the awkward syntax where, e.g., (A B) means (A & B)
            # Must be done in a loop because re.sub does not work on
            # overlapping matches.

            while True:
                psubbed = imprex.sub('\g<1>&\g<2>', psubs)
                if psubbed == psubs:
                    break
                psubs = psubbed

            try:
                # Note: parse_pin may strip parentheses so add double
                # parentheses to make sure the final &1 applies to
                # the whole expression.
                tval = eval('((' + psubs + '))&1')
            except (SyntaxError, NameError):
                tabstr = ''
                print("Could not evaluate function " + cellrec['function'][k])
                print("(Evaluated as " + psubs + ")")
                break

            if tval:
                bitstr = '1'
            else:
                bitstr = '0'

            # For tristate gates, the function will not include the
            # tristate condition, so determine if tristate is active
            # for this set of inputs and set the output to 'Z' if so.
            if triidx < nin:
                if binstring[triidx] == '1' and tripos == True:
                    bitstr = 'Z'
                elif binstring[triidx] == '0' and tripos == False:
                    bitstr = 'Z'

            tabstr += bitstr
    return (nin, nout, tabstr)

################################################



def read_spice_lib(filein, celldefs, debug):
    # Read a spice library of standard cell definitions

    spicedefs = {}

    with open(filein, 'r') as ifile:
        spitext = ifile.read()

    increx = re.compile('^[^\*]*[ \t]*.include[ \t]+([^ \t]+).*$', re.IGNORECASE)
    subrex  = re.compile('^[^\*]*[ \t]*.subckt[ \t]+([^ \t]+)(.*)$', re.IGNORECASE)
    endsrex = re.compile('^[^\*]*[ \t]*.ends.*$', re.IGNORECASE)
    comrex  = re.compile('^[\*]+.*$', re.IGNORECASE)

    # Merge continuation lines in input
    spilines = spitext.replace('\n+', ' ').splitlines()
    if debug:
        print("Reading spice file, " + str(len(spilines)) + " lines.")

    insub = False
    for line in spilines:

        # All comment lines are ignored
        cmatch = comrex.match(line)
        if cmatch:
            continue

        imatch = increx.match(line)
        if imatch:
            # Check if filename needs same prefix as current file
            if not os.path.exists(imatch.group(1)):
                prefix = os.path.split(filein)[0]
                incfile = prefix + '/' + imatch.group(1)
            else:
                incfile = imatch.group(1)
            read_spice_lib(incfile, celldefs, debug)
            continue

        ematch = endsrex.match(line)
        if ematch:
            if insub:
                insub = False
            else:
                print("Error: .ends outside of a subcircuit")
            continue

        if not insub:
            lmatch = subrex.match(line)
            if lmatch:
                subname = lmatch.group(1)
                if subname in celldefs:
                    if debug:
                        print("Found subcircuit " + subname)
                    # Read information on spice port order into celldefs record
                    # Because line extensions have been handled, all information
                    # is on the first line.
                    cellrec = celldefs[subname]
                    cellrec['spicepins'] = lmatch.group(2).split()
                    if debug:
                        print('spicepins is ' + str(cellrec['spicepins']))

                # Read to the ".ends" statement.
                insub = True

def write_models(cellsused, celldefs, ofile, timing):
    # Write the .model statement for all cells used

    io_time = timing[0]
    time = timing[1]
    idelay = timing[2]
    odelay = timing[3]
    cload = timing[4]

    
    #primerex = re.compile('([10\)])[ \t]*\'')
    for cellname in cellsused:
        cellrec = celldefs[cellname]
        # per-cell timing from Liberty, with fallback
        rise_ns, fall_ns = cell_intrinsic_delay(cellrec)

        if rise_ns is not None:
            rise_delay = values_to_spice_unit(rise_ns * TIME_UNIT_SEC)
        else:
            rise_delay = time

        if fall_ns is not None:
            fall_delay = values_to_spice_unit(fall_ns * TIME_UNIT_SEC)
        else:
            fall_delay = time

        if cellrec['input_caps']:
            avg_cap = sum(cellrec['input_caps']) / len(cellrec['input_caps'])
            input_load = values_to_spice_unit(avg_cap * CAP_UNIT_FARAD)
        else:
            input_load = cload
        
        
        if len(cellrec['function']) > 0:
            print('* ' + cellname + ' ' + cellrec['function'][0], file=ofile)
        else:
            print('* ' + cellname + ' (no function)', file=ofile)
        if cellrec['type'] == 'flop':
            rise_ns, fall_ns = cell_intrinsic_delay(cellrec, arc_types=('rising_edge', 'falling_edge'))
            clk_delay = values_to_spice_unit((rise_ns or 0) * TIME_UNIT_SEC) if rise_ns is not None else time
            # If set/reset arcs exist, extract them separately; otherwise fall back to command-line values
            set_delay = time
            reset_delay = time
            # input_load uses the average of DFF input pin capacitances
            data_load = values_to_spice_unit((sum(cellrec['input_caps']) / len(cellrec['input_caps'])) * CAP_UNIT_FARAD) if cellrec['input_caps'] else cload
            print('.model d_dff_' + cellname + ' d_dff(ic=0 clk_delay=' + clk_delay +
                ' rise_delay=' + (values_to_spice_unit(rise_ns * TIME_UNIT_SEC) if rise_ns else time) +
                ' fall_delay=' + (values_to_spice_unit(fall_ns * TIME_UNIT_SEC) if fall_ns else time) +
                ' data_load=' + data_load + ')', file=ofile)
            continue
        elif cellrec['type'] == 'latch':
            continue
        elif cellrec['nin'] == 0:
            continue
        else:
            (nin, nout, tabstr) = generate_lut_table(cellrec)

            if tabstr != '':
                if nout == 1:
                    # General n-input LUT model
                    print('.model d_lut_' + cellname + ' d_lut (rise_delay=' + rise_delay + ' fall_delay=' + fall_delay + ' input_load=' + input_load, file=ofile)
                    print('+ table_values "' + tabstr + '")', file=ofile)
                    if debug:
                        print('Cell ' + cellname + ' has table ' + tabstr)
                else:
                    # Even more general n-input, m-output LUT model
                    idelstr = ((idelay + ' ') * nin).strip() 
                    ilodstr = ((cload + ' ') * nin).strip() 
                    odelstr = ((odelay + ' ') * nout).strip() 
                            
                    print('.model d_genlut_' + cellname + ' d_genlut (', file=ofile)
                    print('+ rise_delay=[' + rise_delay + ']', file=ofile)
                    print('+ fall_delay=[' + fall_delay + ']', file=ofile)
                    print('+ input_load=[' + input_load + ']', file=ofile)
                    print('+ input_delay=[' + idelstr + ']', file=ofile)
                    print('+ table_values "' + tabstr + '")', file=ofile)
            else:
                print('No output for ' + cellname)

def read_spice(filein, fileout, celldefs, debug, modelfile, timing):
    global vdd, computed_slews, net_caps, net_driver, inst_data

    vddname = '{:g}'.format(vdd).replace('.', 'v')
    if not 'v' in vddname:
        vddname += 'v'

    vddthigh = str(2 * vdd / 3)
    vddtlow = str(vdd / 3)
    vddhigh = str(vdd)

    # Read a top-level SPICE file with a digital standard cell subcircuit, and
    # write it back out as a subcircuit of XSPICE models and calls with
    # appropriate A-to-D and D-to-A bridges.

    # If 'modelfile' is given, then do not write out model lines
    # for the digital cells, but just include the modelfile at top.
    
    
    

    with open(filein, 'r') as ifile:
        spitext = ifile.read()

    increx = re.compile('^[^\*]*[ \t]*.include[ \t]+([^ \t]+).*$', re.IGNORECASE)
    subrex = re.compile('^[^\*]*[ \t]*.subckt[ \t]+([^ \t]+)(.*)$', re.IGNORECASE)
    xrex   = re.compile('^[ \t]*X([^ \t]+)(.*)$', re.IGNORECASE)
    comrex = re.compile('^[\*]+.*$', re.IGNORECASE)
    specrex = re.compile('^[\*]+This file may contain array delimiters', re.IGNORECASE)
    endsrex = re.compile('^[^\*]*[ \t]*.ends.*$', re.IGNORECASE)
    endrex = re.compile('^[^\*]*[ \t]*.end.*$', re.IGNORECASE)

    # Merge continuation lines in input
    spilines = spitext.replace('\n+', ' ').splitlines()
    if debug:
        print("Reading spice file, " + str(len(spilines)) + " lines.")

    # Get expected name of subcircuit from filename
    fileroot = os.path.split(filein)[1]
    subname = os.path.splitext(fileroot)[0]

    cellsused = []
    pindefs = {}

    # Make first pass to determine if there is more than one subcircuit in
    # the main file.
    subcount = 0
    subfound = False
    for line in spilines:
        cmatch = comrex.match(line)
        if cmatch:
            continue
        lmatch = subrex.match(line)
        if lmatch:
            subcount += 1
            if lmatch.group(1) == subname:
                subfound = True
    
    print("Writing xspice file")
    with open(fileout, 'w') as ofile:
        print("* XSpice netlist created from SPICE and liberty sources by spi2xspice.py", file=ofile)
        echoout = False
        lineno = 0
        skipsub = False
        for line in spilines:
            lineno += 1

            # Substitute for all characters "[" and "]", to avoid nesting brackets.
            line = line.replace('[', '_').replace(']', '_')

            # All comment lines go to the output, except catch the line generated
            # by qflow scripts that says "This file may contain array delimiters",
            # since we just scrubbed them, above.

            cmatch = comrex.match(line)
            if cmatch:
                smatch = specrex.match(line)
                if not smatch:
                    print(line, file=ofile)
                continue

            # If there is an include line, parse it and read the
            # library cells from that include file.
            imatch = increx.match(line) if not skipsub else []
            if imatch:
                read_spice_lib(imatch.group(1), celldefs, debug)
                #################
                ## Yacine UPDATES: Calculate net loads and driver information for slew calculations
                ################# 
                default_cload = parse_float_from_unit(timing[4])
                default_slew = parse_float_from_unit(timing[0])

                net_caps = calculate_net_loads(spilines, celldefs, default_cload)
                net_driver, inst_data = build_circuit_graph(spilines, celldefs)
                computed_slews.clear() 
                #################
                continue

            xmatch = xrex.match(line) if not skipsub else []
            if xmatch:
                # Replace subcircuit call with xspice call
                instname = xmatch.group(1)
                # NOTE:  Parsing for common CDLisms like '/' before cellname and
		# parameter passing to the instance.
                conns = [item for item in xmatch.group(2).split() if '=' not in item and item != '/']
                pins = conns[0:-1]
                cellname = conns[-1]

                if cellname not in celldefs:
                    print("Could not find cellname " + cellname + " from subcircuit line " + str(lineno))
                    continue

                if cellname not in cellsused:
                    cellsused.append(cellname)

                cellrec = celldefs[cellname]
                # Tricky!  For each pin, find the corresponding subcircuit
                # pin name.  Then look up that pin name in the cell record
                # and find if it is an input or an output.  Then assign
                # the pin direction to the connecting net.  Then compile
                # the list of input and output nets based on the XSPICE
                # model's port order.

                if not 'spicepins' in cellrec:
                    print('Cell ' + cellname + ' does not have SPICE pin order defined!')
                    if debug:
                        print('Cell record is: ' + str(cellrec))
                    continue

                if not 'type' in cellrec:
                    print('Cell ' + cellname + ' does not have a type defined!')
                    if debug:
                        print('Cell record is: ' + str(cellrec))
                    # Provide defaults for the cell so that it doesn't cause failures
                    cellrec['type'] = 'unknown'
                    cellrec['function'] = []
                    cellrec['inputs'] = []
                    cellrec['outputs'] = []
                    cellrec['nin'] = 0
                    cellrec['nout'] = 0

                # Print the record.  Special handling for flops and latches.
                if cellrec['type'] == 'flop':

                    dclk = 'NULL'
                    ddata = 'NULL'
                    dreset = 'NULL'
                    dset = 'NULL'
                    dq = 'NULL'
                    dqbar = 'NULL'

                    if 'clock' in cellrec:
                        clkpin = cellrec['clock']
                        if clkpin[0] == '~':
                            subpin = clkpin[1:]
                        else:
                            subpin = clkpin
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if clkpin[0] == '~':
                            dclk = '~' + pins[i]
                        else:
                            dclk = pins[i]

                    # Normally 'data' will resolve to a pin name.  BUT,
                    # scan flops can have complicated expressions for
                    # next_state.  These should NOT be used by synthesis,
                    # but check for them anyway, so they do not cause
                    # the program to fault.
                    if 'data' in cellrec:
                        datapin = cellrec['data']
                        if datapin[0] == '~':
                            subpin = datapin[1:]
                        else:
                            subpin = datapin
                        try:
                            i = cellrec['spicepins'].index(subpin)
                        except:
                            ddata = 'ERROR'
                            print('Pin ' + subpin + ' of subckt ' + cellname + ' cannot be parsed.', file=sys.stderr)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if datapin[0] == '~':
                            ddata = '~' + pins[i]
                        else:
                            ddata = pins[i]

                    if 'set' in cellrec:
                        setpin = cellrec['set']
                        if setpin[0] == '~':
                            subpin = setpin[1:]
                        else:
                            subpin = setpin
                        if debug:
                            print("flop setpin = " + setpin + " subpin = " + subpin)
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if setpin[0] == '~':
                            dset = '~' + pins[i]
                        else:
                            dset = pins[i]

                    if 'reset' in cellrec:
                        resetpin = cellrec['reset']
                        if resetpin[0] == '~':
                            subpin = resetpin[1:]
                        else:
                            subpin = resetpin
                        if debug:
                            print("flop resetpin = " + resetpin + " subpin = " + subpin)
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if resetpin[0] == '~':
                            dreset = '~' + pins[i]
                        else:
                            dreset = pins[i]

                    for subpin in cellrec['outputs']:
                        subidx = cellrec['outputs'].index(subpin)
                        function = cellrec['function'][subidx]
                        if subpin in cellrec['spicepins']:
                            i = cellrec['spicepins'].index(subpin)
                            if pins[i] in pindefs:
                                pindefs[pins[i]] = 'output'
                            if 'funcneg' in cellrec:
                                if cellrec['funcneg'] == function:
                                    dqbar = pins[i]
                            if 'funcpos' in cellrec:
                                if cellrec['funcpos'] == function:
                                    dq = pins[i]
                        elif debug:
                            print('Pin ' + subpin + ' not in pin list of ' + instname)
                    
                    clk_net = pins[cellrec['spicepins'].index(subpin)] if 'clock' in cellrec else None
                    clk_slew = get_net_slew(clk_net, net_driver, inst_data, celldefs, net_caps, computed_slews, default_slew/TIME_UNIT_SEC) if clk_net else default_slew/TIME_UNIT_SEC
                    
                    # Q output load (using dq net)
                    flop_load = net_caps.get(dq, default_cload) if dq != 'NULL' else default_cload/CAP_UNIT_FARAD
                    
                    # 2. Extract specific DFF timing arcs
                    rise_ns, fall_ns = 1.0, 1.0 # Default fallbacks
                    for arc in cellrec.get('timing_arcs', []):
                        if arc.get('timing_type') in ('rising_edge', 'falling_edge'):
                            if 'cell_rise' in arc['tables'] and 'cell_fall' in arc['tables']:
                                rise_ns = interpolate_2d(arc['tables']['cell_rise']['index_1'], arc['tables']['cell_rise']['index_2'], arc['tables']['cell_rise']['values'], clk_slew, flop_load)
                                fall_ns = interpolate_2d(arc['tables']['cell_fall']['index_1'], arc['tables']['cell_fall']['index_2'], arc['tables']['cell_fall']['values'], clk_slew, flop_load)
                                break
                    
                    # 3. Format values
                    rise_str = values_to_spice_unit(rise_ns * TIME_UNIT_SEC)
                    fall_str = values_to_spice_unit(fall_ns * TIME_UNIT_SEC)
                    data_load_str = values_to_spice_unit((sum(cellrec['input_caps']) / len(cellrec['input_caps']) if cellrec.get('input_caps') else default_cload) * CAP_UNIT_FARAD)

                    # 4. Print Instance and Unique Model
                    print(f"A{instname} {ddata} {dclk} {dset} {dreset} {dq} {dqbar} d_dff_{cellname}_{instname}", file=ofile)
                    print(f".model d_dff_{cellname}_{instname} d_dff(ic=0 clk_delay={rise_str} rise_delay={rise_str} fall_delay={fall_str} data_load={data_load_str})", file=ofile)

                    #print('A' + instname + ' ' + ddata + ' ' + dclk + ' ' + dset + ' ' + dreset + ' ' + dq + ' ' + dqbar + ' d_dff_' + cellname, file=ofile)

                elif cellrec['type'] == 'latch':

                    dena   = 'NULL'
                    ddata  = 'NULL'
                    dreset = 'NULL'
                    dset   = 'NULL'
                    dq     = 'NULL'
                    dqbar  = 'NULL'

                    if 'enable' in cellrec:
                        enapin = cellrec['enable']
                        if enapin[0] == '~':
                            subpin = enapin[1:]
                        else:
                            subpin = enapin
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if enapin[0] == '~':
                            dena = '~' + pins[i]
                        else:
                            dena = pins[i]

                    if 'data' in cellrec:
                        datapin = cellrec['data']
                        if datapin[0] == '~':
                            subpin = datapin[1:]
                        else:
                            subpin = datapin
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if datapin[0] == '~':
                            ddata = '~' + pins[i]
                        else:
                            ddata = pins[i]

                    if 'set' in cellrec:
                        setpin = cellrec['set']
                        if setpin[0] == '~':
                            subpin = setpin[1:]
                        else:
                            subpin = setpin
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if setpin[0] == '~':
                            dset = '~' + pins[i]
                        else:
                            dset = pins[i]

                    if 'reset' in cellrec:
                        resetpin = cellrec['reset']
                        if resetpin[0] == '~':
                            subpin = resetpin[1:]
                        else:
                            subpin = resetpin
                        i = cellrec['spicepins'].index(subpin)
                        if pins[i] in pindefs:
                            pindefs[pins[i]] = 'input'
                        if resetpin[0] == '~':
                            dreset = '~' + pins[i]
                        else:
                            dreset = pins[i]

                    for subpin in cellrec['outputs']:
                        subidx = cellrec['outputs'].index(subpin)
                        function = cellrec['function'][subidx]
                        if subpin in cellrec['spicepins']:
                            i = cellrec['spicepins'].index(subpin)
                            if pins[i] in pindefs:
                                pindefs[pins[i]] = 'output'
                            if 'funcneg' in cellrec:
                                if cellrec['funcneg'] == function:
                                    dqbar = pins[i]
                            if 'funcpos' in cellrec:
                                if cellrec['funcpos'] == function:
                                    dq = pins[i]

                    print('A' + instname + ' ' + ddata + ' ' + dena + ' ' + dset + ' ' + dreset + ' ' + dq + ' ' + dqbar + ' dlatch', file=ofile)

                else:
                    inlist = []
                    outlist = []
                    if 'inputs' in cellrec:
                        for subpin in cellrec['inputs']:
                            subpinname = subpin.strip('"')
                            if subpinname in cellrec['spicepins']:
                                i = cellrec['spicepins'].index(subpinname)
                                # "pins[i]" is the name of the connecting net
                                # on the top level
                                if len(pins) > i:
                                    inlist.append(pins[i])
                                    if pins[i] in pindefs:
                                        if pindefs[pins[i]] != 'output':
                                            pindefs[pins[i]] = 'input'
                                else:
                                    print('Pin ' + subpinname + ' of subckt ' + cellname + ' does not have a connecting net', file=sys.stderr)

                    if 'outputs' in cellrec:
                        for subpin in cellrec['outputs']:
                            subpinname = subpin.strip('"')
                            if subpinname in cellrec['spicepins']:
                                i = cellrec['spicepins'].index(subpinname)
                                # "pins[i]" is the name of the connecting net
                                # on the top level
                                if len(pins) > i:
                                    outlist.append(pins[i])
                                    if pins[i] in pindefs:
                                        pindefs[pins[i]] = 'output'
                                else:
                                    print('Pin ' + subpinname + ' of subckt ' + cellname + ' does not have a connecting net', file=sys.stderr)

                    intext = ' '.join(inlist)
                    outtext = ' '.join(outlist)

                    if debug:
                        print("Instance = " + instname + "; inputs = " + intext + "; outputs = " + outtext)

                    # Note:  Cells with output but no input are tie-high or tie-low,
                    # and do not map to LUTs.  Cells with no output are non-functional
                    # (e.g., antenna tie-downs).
                    
                    #################
                    ## Yacine UPDATES: Handle cells timing
                    #################
                    out_net = outlist[0] if outlist else None
                    inst_load = net_caps.get(out_net, default_cload) if out_net else default_cload/CAP_UNIT_FARAD
                    
                    in_slews = [get_net_slew(in_net, net_driver, inst_data, celldefs, net_caps, computed_slews, default_slew/TIME_UNIT_SEC) 
                                for in_net in inlist]
                    inst_slew = max(in_slews) if in_slews else default_slew/TIME_UNIT_SEC
                    # 2. Calculate Exact Delays
                    arc = pick_primary_arcs(cellrec)[0] if cellrec.get('timing_arcs') else None
                    if arc and 'cell_rise' in arc['tables'] and 'cell_fall' in arc['tables']:
                        rise_ns = interpolate_2d(arc['tables']['cell_rise']['index_1'], arc['tables']['cell_rise']['index_2'], arc['tables']['cell_rise']['values'], inst_slew, inst_load)
                        fall_ns = interpolate_2d(arc['tables']['cell_fall']['index_1'], arc['tables']['cell_fall']['index_2'], arc['tables']['cell_fall']['values'], inst_slew, inst_load)
                    else:
                        rise_ns, fall_ns = 1.0, 1.0 # Fallback

                    rise_str = values_to_spice_unit(rise_ns * TIME_UNIT_SEC)
                    fall_str = values_to_spice_unit(fall_ns * TIME_UNIT_SEC)
                    load_str = values_to_spice_unit(inst_load * CAP_UNIT_FARAD)  
                    
                    ##################
                    
                    if len(inlist) == 0 and len(outlist) == 1:
                        if cellrec['function'][0] == '1':
                            print('A' + instname + ' ' + outtext + ' done', file=ofile)
                        elif cellrec['function'][0] == '0':
                            print('A' + instname + ' ' + outtext + ' dzero', file=ofile)
                        else:
                            print("Cell " + cellname + " has no inputs and no constant function.")
                    elif len(inlist) == 0 and len(outlist) != 0: #FIXME
                    #Multiple outputs with no inputs, it is a cell that is used to tie high or tie low
                        for idx,fcn in enumerate(cellrec['function']):
                            if fcn == '1':
                                print('A' + instname + str(idx) + ' ' + outlist[idx] + ' done', file=ofile)
                            elif fcn == '0':
                                print('A' + instname + str(idx) + ' ' + outlist[idx] + ' dzero', file=ofile)

                    elif len(outlist) == 1:
                        print('A' + instname + ' [' + intext + '] ' + outtext + ' d_lut_' + cellname + f'_{instname}', file=ofile)
                        (_, _, tabstr) = generate_lut_table(cellrec) # You'll need to wrap the truth table logic in a helper
                        if tabstr:
                            print(f".model d_lut_{cellname}_{instname} d_lut (rise_delay={rise_str} fall_delay={fall_str} input_load={load_str}", file=ofile)
                            print(f"+ table_values \"{tabstr}\")", file=ofile)
                    elif len(outlist) != 0:
                        print('A' + instname + ' [' + intext + '] [' + outtext + '] d_genlut_' + cellname, file=ofile)

                continue

            lmatch = subrex.match(line)

            if lmatch:
                subcktname = lmatch.group(1)
                if subcktname != subname and subcount != 1:
                    # Get pin names and pin order in the subcircuit                
                    if subcktname in celldefs:
                        cellrec = celldefs[subcktname]
                    else:
                        cellrec = {}
                        celldefs[subcktname] = cellrec 
                    cellrec['spicepins'] = lmatch.group(2).split()
                    if debug:
                        print('subckt ' + subcktname + ' pins = ' + str(cellrec['spicepins']))
                    skipsub = True

                else:
                    subname = subcktname
                    echoout = False
                    if debug:
                        print("Found subcircuit " + subname)
                    # Prepend all signal names with "a_"
                    pins = lmatch.group(2).split()
                    pindefs = {}
                    # Use pindefs dict to track which pins are input and output
                    for pin in pins:
                        pindefs[pin] = 'unknown'

                    # Write modified .subckt line
                    if debug:
                        print("Rewriting subcircuit " + subname)
                    print(".subckt " + subname, end='', file=ofile)
                    for pin in pins:
                        print(' a_' + pin, end='', file=ofile)
                    print('', file=ofile)

            ematch = endsrex.match(line)
            if ematch and skipsub:
                skipsub = False
            elif ematch:
                if not echoout:
                    # Write A-to-D and D-to-A bridge models
                    print("", file=ofile)
                    if modelfile != '':
                        print(".include " + modelfile, file=ofile)
                    else:
                        print(".model todig_" + vddname + " adc_bridge(in_high=" + vddthigh + " in_low=" + vddtlow + " rise_delay=" + io_time + " fall_delay=" + io_time + ")", file=ofile)
                        print(".model toana_" + vddname + " dac_bridge(out_high=" + vddhigh + " out_low=0)", file=ofile)
                        print("", file=ofile)

                        # Write d_dff, d_dlatch, d_pullup, and d_pulldown models
                        #print(".model ddflop d_dff(ic=0 rise_delay=" + time + " fall_delay=" + time + ")", file=ofile)
                        print(".model dlatch d_dlatch(ic=0 rise_delay=" + time + " fall_delay=" + time + ")", file=ofile)
                        print(".model dzero d_pulldown(load=" + cload + ")", file=ofile)
                        print(".model done d_pullup(load=" + cload + ")", file=ofile)
                        print("", file=ofile)

                    # Write A-to-D and D-to-A bridges
                    inum = 0
                    onum = 0
                    for pinname in pindefs:
                        if pindefs[pinname] == 'output':
                            onum += 1
                            print("AD2A" + str(onum) + " [" + pinname + "] [a_" + pinname + "] toana_" + vddname, file=ofile)
                        # (Previously 'input': this makes sure that even unused
                        # power and ground nets are not left floating.)
                        else:
                            inum += 1
                            print("AA2D" + str(inum) + " [a_" + pinname + "] [" + pinname + "] todig_" + vddname, file=ofile)
                    print("", file=ofile)

                echoout =  True

            if not ematch:
                endline = endrex.match(line)
            else:
                endline = None

            if echoout and not endline:
                print(line, file=ofile)

        # At the end, write all of the LUT-based digital models.
        if modelfile == '':
            print("", file=ofile)
            #write_models(cellsused, celldefs, ofile, timing)
  
        print(".end", file=ofile)

def write_lib(fileout, celldefs, debug, timing):
    global vdd

    vddname = '{:g}'.format(vdd).replace('.', 'v')
    if not 'v' in vddname:
        vddname += 'v'

    vddthigh = str(2 * vdd / 3)
    vddtlow = str(vdd / 3)
    vddhigh = str(vdd)

    # 'timing' is a list of five values:  I/O rise/falltime, cell rise/
    # falltime, cell input delay, cell output delay, and cell load cap.
    # These are all rough averages for the process.

    io_time = timing[0]
    time = timing[1]
    idelay = timing[2]
    odelay = timing[3]
    cload = timing[4]

    # Write a library file containing the models of all of the cells in the
    # liberty file, which can then be included in other xspice simulatable
    # netlists.

    print("Writing xspice model library")
    with open(fileout, 'w') as ofile:
        print("* XSpice library created from liberty sources by spi2xspice.py", file=ofile)
        echoout = False

        print("", file=ofile)
        print(".model todig_" + vddname + " adc_bridge(in_high=" + vddthigh + " in_low=" + vddtlow + " rise_delay=" + io_time + " fall_delay=" + io_time + ")", file=ofile)
        print(".model toana_" + vddname + " dac_bridge(out_high=" + vddhigh + " out_low=0)", file=ofile)
        print("", file=ofile)

        # Write d_dff, d_dlatch, d_pullup, and d_pulldown models
        print(".model ddflop d_dff(ic=0 rise_delay=" + time + " fall_delay=" + time + ")", file=ofile)
        print(".model dzero d_pulldown(load=" + cload + ")", file=ofile)
        print(".model done d_pullup(load=" + cload + ")", file=ofile)

        cellsused = []
        for cell in celldefs:
            cellsused.append(cell)

        # At the end, write all of the LUT-based digital models.
        print("", file=ofile)
        write_models(cellsused, celldefs, ofile, timing)
        print(".end", file=ofile)

def parse_pin(function):
    # Handle n' as way of expressing ~n or !n
    primerex = re.compile('([^ \t]+)[ \t]*\'')
    outparenrex = re.compile('^[ \t]*\([ \t]*(.+)[ \t]*\)[ \t]*$')
    parenrex = re.compile('\([ \t]*([^ \t\)|&~^]+)[ \t]*\)')
    pstring = function.strip().strip('"').strip()
    pstring = pstring.replace('*', '&').replace('+', '|').replace('!', '~')
    pstring = outparenrex.sub('\g<1>', pstring)
    pstring = parenrex.sub('\g<1>', pstring)
    pstring = primerex.sub('~\g<1>', pstring)
    return pstring

def read_liberty(filein, debug):
    global vdd
    
    #############
    ## Yacine UPDATES: Including proper timing calculations from liberty file
    #############
    
    global TIME_UNIT_SEC, CAP_UNIT_FARAD

    cell_rise_rex  = re.compile(r'^\s*cell_rise\s*\(')
    cell_fall_rex  = re.compile(r'^\s*cell_fall\s*\(')
    rise_trans_rex = re.compile(r'^\s*rise_transition\s*\(')
    fall_trans_rex = re.compile(r'^\s*fall_transition\s*\(')
    values_rex     = re.compile(r'^\s*values\s*\(\s*(.*?)\s*\)\s*;', re.DOTALL)
    cap_rex        = re.compile(r'^\s*capacitance\s*:\s*([0-9eE\.\-+]+)\s*;')
    time_unit_rex  = re.compile(r'^\s*time_unit\s*:')
    cap_unit_rex   = re.compile(r'^\s*capacitive_load_unit')
    index1_rex     = re.compile(r'^\s*index_1\s*\(\s*"(.*?)"\s*\)\s*;')
    index2_rex     = re.compile(r'^\s*index_2\s*\(\s*"(.*?)"\s*\)\s*;')
    related_rex    = re.compile(r'^\s*related_pin\s*:\s*"?([^";]+)"?\s*;')
    timing_type_rex= re.compile(r'^\s*timing_type\s*:\s*"?([^";]+)"?\s*;')
    timing_open_rex= re.compile(r'^\s*timing\s*\(\s*\)\s*\{')
    direction_rex = re.compile(r'^\s*direction\s*:\s*"([^"]+)"')

    ##############
    celldefs = {}
    voltrex  = re.compile('[ \t]*nom_voltage[ \t]*:[ \t]*([^;]+);')
    cellrex  = re.compile('[ \t]*cell[ \t]*\(([^)]+)\)')
    pinrex   = re.compile('[ \t]*pin[ \t]*\(([^)]+)\)')
    busrex   = re.compile('[ \t]*bus[ \t]*\(([^)]+)\)')
    lat1rex  = re.compile('[ \t]*latch[ \t]*\(([^)]+)\)')
    lat2rex  = re.compile('[ \t]*latch[ \t]*\(([^, \t]+)[ \t]*,[ \t]*([^),]+)\)')
    ff1rex   = re.compile('[ \t]*ff[ \t]*\(([^)]+)\)')
    ff2rex   = re.compile('[ \t]*ff[ \t]*\(([^, \t]+)[ \t]*,[ \t]*([^),]+)\)')
    staterex = re.compile('[ \t]*next_state[ \t]*:[ \t]*([^;]+);')
    clockrex = re.compile('[ \t]*clocked_on[ \t]*:[ \t]*([^;]+);')
    setrex   = re.compile('[ \t]*preset[ \t]*:[ \t]*([^;]+);')
    resetrex = re.compile('[ \t]*clear[ \t]*:[ \t]*([^;]+);')
    datarex  = re.compile('[ \t]*data_in[ \t]*:[ \t]*([^;]+);')
    enarex   = re.compile('[ \t]*enable[ \t]*:[ \t]*([^;]+);')
    trirex   = re.compile('[ \t]*three_state[ \t]*:[ \t]*([^;]+);')
    funcrex  = re.compile('[ \t]*function[ \t]*:[ \t]*\"?[ \t]*([^"]+)[ \t]*\"?')
    
    with open(filein, 'r') as ifile:
        raw = ifile.read()
        raw = re.sub(r'\\\s*\n', ' ', raw)   # collapse line continuations
        lines = raw.splitlines()
        cellrec = {}
        if debug:
            print("Reading liberty file, " + str(len(lines)) + " lines.")
        for line in lines:
            vmatch = voltrex.match(line)
            if vmatch:
                vdd = float(vmatch.group(1))
                if debug:
                   print("Nominal process voltage is " + str(vdd))
                continue

            lmatch = cellrex.match(line)
            if lmatch:
                cellname = lmatch.group(1).strip('"')
                if debug:
                    print("Found cell " + cellname)
                cellrec = {}
                cellrec['name'] = cellname
                cellrec['inputs'] = []
                cellrec['outputs'] = []
                cellrec['nin'] = 0
                cellrec['nout'] = 0
                cellrec['function'] = []
                # NOTE:  average rise and fall times need to be
                # averaged from the data, to get a general relation
                # between timing and drive strength.
                cellrec['rise'] = 1.0
                cellrec['fall'] = 1.0
                cellrec['type'] = 'comb'
                celldefs[cellname] = cellrec
                
                ## Yacine UPDATES: Including proper timing calculations from liberty file
                cellrec['rise_values'] = []
                cellrec['fall_values'] = []
                cellrec['rise_trans_values'] = []
                cellrec['fall_trans_values'] = []
                cellrec['input_caps'] = []
                cellrec['timing_arcs'] = []
                current_arc = None
                current_index1 = None
                current_index2 = None
                current_table = None
                
                continue
            
            if time_unit_rex.match(line):
                v = parse_unit(line)
                if v: TIME_UNIT_SEC = v
                continue
            if cap_unit_rex.match(line):
                v = parse_unit(line)
                if v: CAP_UNIT_FARAD = v
                continue
            
            if not cellrec:
                continue
                
            pmatch = pinrex.match(line)
            if pmatch:
                current_pin = pmatch.group(1).strip('"')
                if debug:
                    print("Found input pin " + current_pin)
                cellrec['inputs'].append(current_pin)
                cellrec['nin'] += 1
                continue

            bmatch = busrex.match(line)
            if bmatch:
                current_pin = bmatch.group(1).strip('"')
                if debug:
                    print("Found input bus " + current_pin)
                cellrec['inputs'].append(current_pin)
                cellrec['nin'] += 1
                continue

            lmatch = lat2rex.match(line)
            if lmatch:
                if debug:
                    print("Found latch");
                cellrec['type'] = 'latch'
                cellrec['funcpos'] = lmatch.group(1).strip('"')
                cellrec['funcneg'] = lmatch.group(2).strip('"')
                continue

            lmatch = lat2rex.match(line)
            if lmatch:
                if debug:
                    print("Found latch");
                cellrec['type'] = 'latch'
                cellrec['funcpos'] = lmatch.group(1).strip('"')
                continue

            rmatch = ff2rex.match(line)
            if rmatch:
                if debug:
                    print("Found flop");
                cellrec['type'] = 'flop'
                cellrec['funcpos'] = rmatch.group(1).strip('"')
                cellrec['funcneg'] = rmatch.group(2).strip('"')
                continue
            else:
                rmatch = ff1rex.match(line)
                if rmatch:
                    if debug:
                        print("Found flop");
                    cellrec['type'] = 'flop'
                    cellrec['funcpos'] = rmatch.group(1).strip('"')
                    continue

            fmatch = funcrex.match(line)
            if fmatch:
                function = fmatch.group(1)
                if debug:
                    print("Found function " + function + " and output pin " + current_pin)
                # If pin has a function, it's an output, not an input,
                # so add it to the outputs list and remove it from the
                # inputs list.
                if current_pin not in cellrec['outputs']:
                    cellrec['outputs'].append(current_pin)
                    cellrec['nout'] += 1
                if current_pin in cellrec['inputs']:
                    cellrec['inputs'].remove(current_pin)
                    cellrec['nin'] -= 1
                cellrec['function'].append(function)
                continue

            smatch = staterex.match(line)
            if smatch:
                if debug:
                    print('Found data input')
                cellrec['data'] = parse_pin(smatch.group(1))
                continue

            cmatch = clockrex.match(line)
            if cmatch:
                if debug:
                    print('Found clock input')
                cellrec['clock'] = parse_pin(cmatch.group(1))
                continue

            smatch = setrex.match(line)
            if smatch:
                if debug:
                    print('Found set input ' + smatch.group(1))
                cellrec['set'] = parse_pin(smatch.group(1))
                continue

            rmatch = resetrex.match(line)
            if rmatch:
                if debug:
                    print('Found reset input ' + rmatch.group(1))
                cellrec['reset'] = parse_pin(rmatch.group(1))
                continue

            dmatch = datarex.match(line)
            if dmatch:
                if debug:
                    print('Found data input')
                cellrec['data'] = parse_pin(dmatch.group(1))
                continue

            ematch = enarex.match(line)
            if ematch:
                if debug:
                    print('Found enable input')
                cellrec['enable'] = parse_pin(ematch.group(1))
                continue

            tmatch = trirex.match(line)
            if tmatch:
                if debug:
                    print('Found tristate output')
                cellrec['tristate'] = parse_pin(tmatch.group(1))
                continue
            
            
            ## handle timing information
            if timing_open_rex.match(line):
                current_arc = {'related_pin': None, 'timing_type': None, 'tables': {}}
                cellrec['timing_arcs'].append(current_arc)
                current_table = None
                continue
            
            m = related_rex.match(line)
            if m and current_arc is not None:
                current_arc['related_pin'] = m.group(1).strip()
                continue
            m = timing_type_rex.match(line)
            if m and current_arc is not None:
                current_arc['timing_type'] = m.group(1).strip()
                continue
            
            m = direction_rex.match(line)
            if m:
                direction = m.group(1).strip().lower()
                cellrec['is_output_pin'] = (direction == 'output')
                if direction == 'output' and current_pin in cellrec['inputs']:
                    cellrec['inputs'].remove(current_pin)
                    cellrec['nin'] -= 1
                    cellrec['outputs'].append(current_pin)
                    cellrec['nout'] += 1
                continue
            
            vmatch = re.search(values_rex, line) or values_rex.match(line)
            if vmatch and current_table:
                nums = [float(x) for x in re.findall(r'[0-9eE\.\-+]+', vmatch.group(1))]
                entry = current_arc['tables'].setdefault(current_table, {})
                entry['index_1'] = current_index1
                entry['index_2'] = current_index2
                entry['values']  = nums
                continue
            
            cmatch = cap_rex.match(line)
            if cmatch and not cellrec.get('is_output_pin', False):
                cellrec['input_caps'].append(float(cmatch.group(1)))
                continue
            
            m = index1_rex.match(line)
            if m and current_table:
                current_index1 = [float(x) for x in m.group(1).split(',')]
                continue
            m = index2_rex.match(line)
            if m and current_table:
                current_index2 = [float(x) for x in m.group(1).split(',')]
                continue
            
            m = index1_rex.match(line)
            if m and current_table:
                current_index1 = [float(x) for x in m.group(1).split(',')]
                continue
            m = index2_rex.match(line)
            if m and current_table:
                current_index2 = [float(x) for x in m.group(1).split(',')]
                continue
            
            if cell_rise_rex.match(line):  current_table = 'cell_rise';       current_index1 = None; current_index2 = None; continue
            if cell_fall_rex.match(line):  current_table = 'cell_fall';       current_index1 = None; current_index2 = None; continue
            if rise_trans_rex.match(line): current_table = 'rise_transition'; current_index1 = None; current_index2 = None; continue
            if fall_trans_rex.match(line): current_table = 'fall_transition'; current_index1 = None; current_index2 = None; continue
            if line.strip().startswith('}'):
                current_table = None; continue
                

    return celldefs


if __name__ == '__main__':

    options = []
    arguments = []
    for item in sys.argv[1:]:
        if item.find('-', 0) == 0:
            options.append(item)
        else:
            arguments.append(item)

    if '-debug' in options:
        debug = True
    else:
        debug = False

    # Timing options:  Pass timing and load values as the following:
    #
    #   -io_time=<value>  Rise and fall time for signals in and out of the digital block
    #   -time=<value>     Rise and fall time of gate outputs
    #   -idelay=<value>   Input delay at gate inputs
    #   -odelay=<value>   Throughput delay of the gate
    #   -cload=<value>    Gate output load capacitance
    #
    # Note that these are just average numbers for the process.  If not
    # specified, the defaults are as assigned below.

    io_time = '10n'
    time = '1n'
    idelay = '1n'
    odelay = '50n'
    cload = '1p'

    try:
        iotimeopt = next(item for item in options if item.startswith('-io_time='))
    except:
        pass
    else:
        io_time = iotimeopt.split('=')[1]

    try:
        timeopt = next(item for item in options if item.startswith('-time='))
    except:
        pass
    else:
        time = timeopt.split('=')[1]

    try:
        idelayopt = next(item for item in options if item.startswith('-idelay='))
    except:
        pass
    else:
        idelay = idelayopt.split('=')[1]

    try:
        odelayopt = next(item for item in options if item.startswith('-odelay='))
    except:
        pass
    else:
        odelay = odelayopt.split('=')[1]

    try:
        cloadopt = next(item for item in options if item.startswith('-cload='))
    except:
        pass
    else:
        cload = cloadopt.split('=')[1]

    timing = [io_time, time, idelay, odelay, cload]

    vdd = 3.0
    celldefs = {}
    if len(arguments) >= 3:
        print("Reading liberty netlist " + arguments[0])
        print("Reading spice netlist " + arguments[1])
        print("Writing xspice netlist " + arguments[2])
        for libfile in arguments[0].split():
            celldefs.update(read_liberty(libfile, debug))
        if len(arguments) >= 4:
            modelfile = arguments[3]
        else:
            modelfile = ''
        read_spice(arguments[1], arguments[2], celldefs, debug, modelfile, timing)
        print("Done.")
    elif len(arguments) == 2:
        # Library-only option
        print("Reading liberty netlist " + arguments[0])
        print("Writing xspice model library " + arguments[1])
        for libfile in arguments[0].split():
            celldefs.update(read_liberty(libfile, debug))
        write_lib(arguments[1], celldefs, debug, timing)
        print("Done.")
    else:
        print("Usage:")
        print("spi2xspice.py <liberty file> <input spice> <output spice> [<xspice lib>]")
        print("spi2xspice.py <liberty file> <output xspice lib>")




