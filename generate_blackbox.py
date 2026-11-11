import sys

def generate_blackbox(spice_lib_file, output_v_file):
    with open(spice_lib_file, 'r') as f_in, open(output_v_file, 'w') as f_out:
        f_out.write("// Auto-generated Verilog blackbox for Yosys SPICE port ordering\n\n")
        
        for line in f_in:
            # Find every subcircuit definition
            if line.strip().lower().startswith('.subckt'):
                tokens = line.strip().split()
                cell_name = tokens[1]
                
                # Everything after the cell name is a pin. 
                # Filter out VDD and VSS (case-insensitive).
                pins = [p for p in tokens[2:] if p.upper() not in ('VDD', 'VSS')]
                
                pin_list = ", ".join(pins)
                f_out.write(f"module {cell_name} ({pin_list});\n")
                
                # Declare all pins as inout. Yosys will figure out the real 
                # directions (input/output) later when it reads the .lib file.
                if pins:
                    f_out.write(f"    inout {pin_list};\n")
                f_out.write(f"endmodule\n\n")

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python3 make_blackbox.py <input_spice_lib> <output_verilog>")
        sys.exit(1)
    generate_blackbox(sys.argv[1], sys.argv[2])