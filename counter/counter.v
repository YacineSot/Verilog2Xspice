module counter (
    input wire reset_n,
    input wire clk,
    output reg [3:0] count
);

always @(posedge clk or negedge reset_n) begin
    if (!reset_n)
        count <= 4'b0000;
    else
        count <= count + 1;
end
    
endmodule