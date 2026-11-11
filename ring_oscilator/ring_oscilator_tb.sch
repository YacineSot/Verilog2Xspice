v {xschem version=3.4.8RC file_version=1.3}
G {}
K {}
V {}
S {}
F {}
E {}
B 2 220 -530 1020 -130 {flags=graph
y1=0
y2=2
ypos1=0
ypos2=2
divy=5
subdivy=1
unity=1
x1=1.0621511e-08
x2=1.3642417e-08
divx=5
subdivx=1
xlabmag=1.0
ylabmag=1.0
legendmag=1.0
node=a_osc
color=10
dataset=-1
unitx=1
logx=0
logy=0
rawfile=$netlist_dir/ring_oscilator_tb.raw
sim_type=tran
autoload=1
digital=0
hilight_wave=-1}
N -170 -680 -120 -680 {lab=a_VDD}
N -170 -660 -120 -660 {lab=a_VSS}
N -170 -640 -120 -640 {lab=a_en}
N -170 -620 -120 -620 {lab=a_osc}
C {ring_oscilator/output/ihp-sg13cmos5l/ring_oscilator.sym} -100 -690 0 0 {name=X1}
C {lab_pin.sym} -170 -680 0 0 {name=p1 lab=a_VDD}
C {lab_pin.sym} -170 -660 0 0 {name=p2 lab=a_VSS}
C {lab_pin.sym} -170 -640 0 0 {name=p3 lab=a_en}
C {lab_pin.sym} -170 -620 0 0 {name=p4 lab=a_osc}
C {code_shown.sym} -900 -480 0 0 {name=s1 only_toplevel=false
format="tcleval( @value )"
value="
.include  $::IP_ROOT/ring_oscilator/output/ihp-sg13cmos5l/ring_oscilator_1v20_25C.lib
//.include  $::IP_ROOT/ring_oscilator/output/ihp-sg13cmos5l/ring_oscilator_transistor_level.spice
.option rshunt=1G
VDD a_VDD 0 1.2
VSS a_VSS 0 0
VEN a_en 0 1.2
Cload a_osc 0 10f

"}
C {code_shown.sym} -760 -230 0 0 {name=s2 only_toplevel=false value="

.control
	save v(a_osc)
	tran 1n 100u uic
	write ring_oscilator_tb.raw
.endc

"}
C {launcher.sym} 280 -40 0 0 {name=h5
descr="load waves"
tclcommand="xschem raw_read $netlist_dir/ring_oscilator_tb.raw tran"
}
C {simulator_commands_shown.sym} -830 -790 0 0 {
name=Libs_Ngspice
simulator=ngspice
only_toplevel=false
value="
.lib cornerMOSlv.lib mos_tt
.lib cornerMOShv.lib mos_tt
.lib cornerRES.lib res_typ
.lib cornerDIO.lib dio_tt
"
      }
