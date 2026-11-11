v {xschem version=3.4.8RC file_version=1.3}
G {}
K {}
V {}
S {}
F {}
E {}
B 2 190 -700 990 -300 {flags=graph
y1=0
ypos1=0
ypos2=2
divy=5
subdivy=4
unity=1
x1=0
x2=4e-08
divx=5
subdivx=4
xlabmag=1.0
ylabmag=1.0
legendmag=1.0
node="a_clk
a_reset_n
count;count[3],count[2],count[1],count[0]

count[0]
count[1]
count[2]
count[3]"
color="4 5 6 7 8 9 10"
dataset=-1
unitx=1
logx=0
logy=0
digital=1
rainbow=1
legend=1
y2=1.2}
B 2 990 -700 1790 -300 {flags=graph
y1=0
y2=1.2
ypos1=0
ypos2=2
divy=5
subdivy=1
unity=1
x1=0
x2=40n
divx=5
subdivx=1
xlabmag=1.0
ylabmag=1.0
legendmag=1.0
node=count[0]
color=13
dataset=-1
unitx=1
logx=0
logy=0
}
N -10 -30 30 -30 {lab=a_VDD}
N -10 -10 30 -10 {lab=a_VSS}
N -10 10 30 10 {lab=a_reset_n}
N -10 30 30 30 {lab=a_clk}
N -10 50 30 50 {lab=#net1}
N -10 70 30 70 {lab=#net2}
N -10 90 30 90 {lab=#net3}
N -10 110 30 110 {lab=#net4}
N -140 50 -70 50 {lab=count[0]}
N -140 70 -70 70 {lab=count[1]}
N -140 90 -70 90 {lab=count[2]}
N -140 110 -70 110 {lab=count[3]}
C {counter/output/ihp-sg13cmos5l/counter.sym} 50 -40 0 0 {name=X1}
C {code_shown.sym} -680 -360 0 0 {name=s1 only_toplevel=false
format="tcleval( @value )"
value="
.include  $::IP_ROOT/counter/output/ihp-sg13cmos5l/counter_1v20_25C.lib

VDD a_VDD 0 1.2
VSS a_VSS 0 0
VRST a_reset_n 0 pulse (1.2 0 0 1p 1p 1n 200n)
VCLK a_clk 0 pulse(0 1.2 0 1p 1p 0.5n 1n)

"}
C {lab_pin.sym} -10 -30 0 0 {name=p1 lab=a_VDD}
C {lab_pin.sym} -10 -10 0 0 {name=p2 lab=a_VSS}
C {lab_pin.sym} -10 10 0 0 {name=p3 lab=a_reset_n}
C {lab_pin.sym} -10 30 0 0 {name=p4 lab=a_clk}
C {lab_pin.sym} -140 50 0 0 {name=p5 lab=count[0]}
C {lab_pin.sym} -140 70 0 0 {name=p6 lab=count[1]}
C {lab_pin.sym} -140 90 0 0 {name=p7 lab=count[2]}
C {lab_pin.sym} -140 110 0 0 {name=p8 lab=count[3]}
C {code_shown.sym} -650 -110 0 0 {name=s2 only_toplevel=false value="

.save all
.control
	tran 1p 100n
	write counter_tb.raw
.endc

"}
C {ammeter.sym} -40 50 1 0 {name=Vmeas savecurrent=true spice_ignore=0}
C {ammeter.sym} -40 70 1 0 {name=Vmeas1 savecurrent=true spice_ignore=0}
C {ammeter.sym} -40 90 1 0 {name=Vmeas2 savecurrent=true spice_ignore=0}
C {ammeter.sym} -40 110 1 0 {name=Vmeas3 savecurrent=true spice_ignore=0}
C {launcher.sym} 520 -170 0 0 {name=h5
descr="load waves"
tclcommand="xschem raw_read $netlist_dir/counter_tb.raw tran"
}
