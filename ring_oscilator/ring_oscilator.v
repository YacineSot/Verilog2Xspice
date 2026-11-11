module ring_oscilator #(
    parameter integer STAGES = 100
) (
    input  wire en,
    output wire osc
);
    localparam integer N = (STAGES % 2 == 1) ? STAGES : STAGES + 1;

    // Keep the wires from being optimized away
    (* keep = 1 *) wire [N-1:0] n;
    
    // Explicitly instantiate the IHP 130nm NAND gate cell
    (* keep = 1 *) sg13cmos5l_nand2_1 u_nand (
        .A(en),
        .B(n[N-1]),
        .Y(n[0])
    );

    genvar i;
    generate
        for (i = 1; i < N; i = i + 1) begin : g_inv
            // Explicitly instantiate the IHP 130nm inverter cell
            (* keep = 1 *) sg13cmos5l_inv_1 u_inv (
                .A(n[i-1]),
                .Y(n[i])
            );
        end
    endgenerate

    assign osc = n[N-1];
endmodule