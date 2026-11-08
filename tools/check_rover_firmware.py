#!/usr/bin/env python3
"""Compile the actual rover sketch with fake Arduino I/O and exercise navigation.

No device, network, model or Arduino installation is used. Requires a C++17 compiler.
Use --sysroot when the local compiler needs an explicit macOS SDK.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ARDUINO = r"""
#pragma once
#include <cassert>
#include <cstdint>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <sstream>
#include <iomanip>
#include <deque>
#include <string>
using __FlashStringHelper = char;
using std::isfinite;
#define F(x) x
#define _BV(x) (1 << (x))
constexpr int A4=4, LOW=0, HIGH=1, INPUT=0, OUTPUT=1, RISING=1;
constexpr int SLOT1=1, SLOT2=2, DIRECT_MODE=0;
constexpr int WGM10=0, CS11=1, WGM12=2, WGM21=3, WGM20=4, CS21=5;
int TCCR1A=0,TCCR1B=0,TCCR2A=0,TCCR2B=0;
unsigned long testMillis=10000, testEcho=5800, testedTimeout=0, testPulseDelayMs=0;
int testedPin=-1;
unsigned long millis(){return testMillis;}
void pinMode(int,int){}
void digitalWrite(int,int){}
int digitalRead(int){return 0;}
int analogRead(int){return 512;}
void delayMicroseconds(int){}
unsigned long pulseIn(int pin,int,unsigned long timeout){testedPin=pin;testedTimeout=timeout;testMillis+=testPulseDelayMs;return testEcho;}
void attachInterrupt(int,void(*)(),int){}
struct Port { uint8_t s1=0,s2=0; };
Port mePort[17];
struct MeEncoderOnBoard {
 long position=0; int pwm=0, zeroWrites=0;
 explicit MeEncoderOnBoard(int){}
 void setMotorPwm(int p){pwm=p;if(p==0)++zeroWrites;}
 void setMotionMode(int){}
 void pulsePosMinus(){--position;}
 void pulsePosPlus(){++position;}
 long getPulsePos(){return position;}
 int getPortB(){return 0;}
 int getIntNum(){return 0;}
 void setPulse(float){}
 void setRatio(float){}
};
struct MeRGBLed {
 MeRGBLed(int,int){}
 void setpin(int){}
 void setColor(int,int,int,int){}
 void show(){}
};
struct SerialMock {
 std::ostringstream out; std::deque<int> input;
 void begin(int){}
 int available(){return input.size();}
 int read(){int b=input.front();input.pop_front();return b;}
 template<class T> void print(T x){out<<x;}
 void print(float x,int precision){std::ostringstream v;v<<std::fixed<<std::setprecision(precision)<<x;out<<v.str();}
 void println(){out<<'\n';}
 template<class T> void println(T x){print(x);println();}
 void println(float x,int precision){print(x,precision);println();}
 void clear(){out.str("");out.clear();}
} Serial;
"""

EEPROM = r"""
#pragma once
struct EepromMock {
 unsigned char data[4096]={};
 template<class T> void get(int i,T &v){memcpy(&v,data+i,sizeof(v));}
 template<class T> void put(int i,const T &v){memcpy(data+i,&v,sizeof(v));}
 int length(){return sizeof(data);}
 unsigned char read(int i){return data[i];}
 void update(int i,unsigned char v){data[i]=v;}
} EEPROM;
"""

CHECKS = r"""
void command(const char *value){
 char text[96];assert(strlen(value)<sizeof(text));strcpy(text,value);handleLine(text);
}
void off(){assert(!moving && !exploring && leftMotor.pwm==0 && rightMotor.pwm==0);}
void reset(){
 stopMotors();navigationConfigured=false;testMillis=10000;testEcho=5800;testPulseDelayMs=0;
 leftMotor.position=rightMotor.position=0;navigationTurns=0;Serial.clear();
}
void configure(){command("NAVCFG 10 30 45 20 45 8 80 200 15 800 2");assert(navigationConfigured);}
void start(){configure();command("EXPLORE 120 30");assert(exploring && !moving);Serial.clear();}
void tick(unsigned long ms){testMillis+=ms;lastCommandMs=testMillis;loop();}
void completeSegment(){
 leftMotor.position=startLeft+targetPulses*leftDirection;
 rightMotor.position=startRight+targetPulses*rightDirection;
 loop();assert(!moving && exploring && navigationPhase==NAV_WAIT);
}
int main(){
 for(int p=0;p<17;p++)mePort[p].s2=100+p;
 setup();
 // Continuous guarded travel crosses 3 cm without stopping; only final target stops.
 reset();configure();command("CRUISE 50 35 15000");assert(moving && continuousMove);
 tick(20);int cruiseZeroLeft=leftMotor.zeroWrites,cruiseZeroRight=rightMotor.zeroWrites;
 for(int i=0;i<40;i++){
   leftMotor.position+=10*leftDirection;rightMotor.position+=10*rightDirection;
   tick(80);assert(moving && leftMotor.pwm!=0 && rightMotor.pwm!=0);
 }
 assert(leftMotor.zeroWrites==cruiseZeroLeft && rightMotor.zeroWrites==cruiseZeroRight);
 assert(Serial.out.str().find("NAV FORWARD")!=std::string::npos);
 leftMotor.position=startLeft+targetPulses*leftDirection;
 rightMotor.position=startRight+targetPulses*rightDirection;
 tick(20);off();assert(Serial.out.str().find("OK CRUISE 50 35 15000")!=std::string::npos);
 reset();configure();command("CRUISE 50 35 15000");testEcho=25*58;tick(80);off();
 assert(Serial.out.str().find("ERR obstacle_too_close")!=std::string::npos);
 reset();configure();command("CRUISE 50 35 15000");testEcho=0;tick(80);off();
 assert(Serial.out.str().find("ERR range_unknown")!=std::string::npos);
 reset();configure();command("CRUISE 50 35 15000");testMillis+=1500;loop();off();
 assert(Serial.out.str().find("ERR watchdog")!=std::string::npos);
 reset();configure();command("CRUISE 50 35 100");tick(100);off();
 assert(Serial.out.str().find("ERR guard_timeout")!=std::string::npos);
 reset();configure();command("STEP 50 35 15000");off(); // Old short-action ceiling preserved.
 // Mechanical forward points toward the front sensor, with matching encoder signs.
 reset();command("DRIVE 10 30");tick(20);
 assert(leftDirection==-1 && rightDirection==1 && leftMotor.pwm<0 && rightMotor.pwm>0);
 command("STOP");off();
 reset();command("DRIVE -10 30");tick(20);
 assert(leftDirection==1 && rightDirection==-1 && leftMotor.pwm>0 && rightMotor.pwm<0);
 command("STOP");off();
 reset();command("TURN 45 30");tick(20);
 assert(leftDirection==1 && rightDirection==1 && leftMotor.pwm>0 && rightMotor.pwm>0);
 command("STOP");off();
 // The relocated sensor is explicitly selectable without changing motion direction.
 reset();command("RANGE 6");off();assert(testedPin==106);
 assert(Serial.out.str()=="RANGE port=6 cm=100.00 valid=1\n");
 // Stationary range reads the specified sensor, uses a bounded pulse wait, never moves.
 reset();command("RANGE 10");off();
 assert(testedPin==110 && testedTimeout==24000);
 assert(Serial.out.str()=="RANGE port=10 cm=100.00 valid=1\n");
 testEcho=0;Serial.clear();command("RANGE 10");off();
 assert(Serial.out.str()=="RANGE port=10 cm=-1.00 valid=0\n");
 // Reject motor-power ports, missing config, and unsafe navigation parameters.
 reset();command("RANGE 1");off();assert(Serial.out.str()=="ERR range_port\n");
 reset();command("EXPLORE 120 30");off();assert(Serial.out.str()=="ERR navigation_not_configured\n");
 reset();command("NAVCFG 10 30 20 20 45 8 80 200 15 800 2");off();assert(!navigationConfigured);
 reset();configure();command("EXPLORE 601 30");off();
 // Forward -> obstacle STOP -> settled turn -> clear forward. No internal move ACKs.
 reset();start();tick(199);assert(!moving);tick(1);assert(moving && leftMotor.pwm==0);tick(20);
 assert(moving && navigationPhase==NAV_FORWARD && leftMotor.pwm<0 && leftMotor.pwm>-5 && rightMotor.pwm>0);
 assert(desiredPwm(targetPulses-5)==maxPwm); // no final straight-drive power reduction
 testEcho=25*58;tick(80);assert(!moving && exploring && leftMotor.pwm==0 && rightMotor.pwm==0);
 assert(Serial.out.str().find("NAV OBSTACLE 25.00")!=std::string::npos);
 tick(200);assert(moving && navigationPhase==NAV_TURN);
 assert(desiredPwm(targetPulses-5)==maxPwm); // avoid low-power exploration-turn stalls
 completeSegment();testEcho=100*58;tick(200);
 assert(moving && navigationPhase==NAV_FORWARD);
 assert(Serial.out.str().find("OK DRIVE")==std::string::npos);
 assert(Serial.out.str().find("OK TURN")==std::string::npos);
 // STOP via the real serial parser has priority over navigation and completion.
 for(char c:std::string("STOP\n"))Serial.input.push_back(c);
 loop();off();tick(1000);off();assert(Serial.out.str().find("OK STOP")!=std::string::npos);
 assert(Serial.out.str().find("OK EXPLORE")==std::string::npos);
 // Forward checkpoints never write zero power, even if one wheel arrives first.
 reset();start();tick(200);tick(20);Serial.clear();
 int zeroLeft=leftMotor.zeroWrites,zeroRight=rightMotor.zeroWrites;
 leftMotor.position=startLeft+targetPulses*leftDirection;
 rightMotor.position=startRight+(targetPulses-10)*rightDirection;
 tick(20);assert(moving && navigationPhase==NAV_FORWARD && leftMotor.pwm<0 && rightMotor.pwm>0);
 assert(startLeft==0); // Wait for both encoder checkpoints before rebasing.
 rightMotor.position=startRight+targetPulses*rightDirection;tick(20);
 assert(startLeft==leftMotor.position && startRight==rightMotor.position);
 assert(leftMotor.zeroWrites==zeroLeft && rightMotor.zeroWrites==zeroRight);
 assert(Serial.out.str().empty());
 // Continuous travel can cross the old 30 s segment deadline with real encoder progress.
 for(int i=0;i<40;i++){
   leftMotor.position+=targetPulses*leftDirection;rightMotor.position+=targetPulses*rightDirection;
   tick(1000);assert(moving && navigationPhase==NAV_FORWARD);
 }
 assert(leftMotor.zeroWrites==zeroLeft && rightMotor.zeroWrites==zeroRight);
 command("STOP");off();
 // Passing one checkpoint never exempts a wheel from the stall guard.
 reset();start();tick(200);
 leftMotor.position=startLeft+targetPulses*2*leftDirection;
 tick(1500);off();assert(Serial.out.str().find("ERR stall slots=2")!=std::string::npos);
 // Missing echo cuts power immediately and keeps it off while retrying.
 reset();start();tick(200);tick(20);testEcho=0;Serial.clear();tick(80);
 assert(!moving && exploring && navigationPhase==NAV_RETRY && leftMotor.pwm==0 && rightMotor.pwm==0);
 assert(Serial.out.str()=="NAV SENSOR_WAIT -1.00\n");
 testEcho=100*58;tick(199);assert(!moving && navigationGoodReads==0);
 tick(1);assert(!moving && navigationGoodReads==1);
 tick(80);assert(moving && navigationPhase==NAV_FORWARD && leftMotor.pwm==0);
 assert(Serial.out.str().find("NAV SENSOR_READY 100.00")!=std::string::npos);
 tick(20);assert(leftMotor.pwm<0 && rightMotor.pwm>0); // gradual restart
 // Persistent loss aborts without restarting, both at startup and while cruising.
 reset();start();testEcho=0;tick(200);assert(exploring && !moving);tick(800);off();
 assert(Serial.out.str().find("ERR range_unknown")!=std::string::npos);
 reset();start();tick(200);testEcho=0;tick(80);tick(800);off();
 assert(Serial.out.str().find("ERR range_unknown")!=std::string::npos);
 // Valid readings must be consecutive and agree on whether forward clearance exists.
 reset();start();testEcho=0;tick(200);testEcho=100*58;tick(200);assert(!moving);
 testEcho=0;tick(80);assert(navigationGoodReads==0);
 testEcho=100*58;tick(80);assert(!moving);testEcho=25*58;tick(80);assert(!moving);
 tick(80);assert(moving && navigationPhase==NAV_TURN);command("STOP");off();
 // STOP or duration expiry during retry cannot be undone by a later valid echo.
 reset();start();testEcho=0;tick(200);command("STOP");off();testEcho=100*58;tick(500);off();
 reset();configure();command("EXPLORE 1 30");tick(200);testEcho=0;tick(80);
 testEcho=100*58;Serial.clear();tick(720);off();assert(Serial.out.str()=="OK EXPLORE 1\n");
 // A valid echo that arrives after the recovery deadline cannot restart motors.
 reset();start();testEcho=0;tick(200);testEcho=100*58;tick(200);assert(!moving);
 testMillis=navigationRetryStartMs+navigationRetryMs-10;lastCommandMs=testMillis;
 testPulseDelayMs=20;loop();off();assert(Serial.out.str().find("ERR range_unknown")!=std::string::npos);
 // Too-close recovery is stopped, not treated as permission to turn.
 reset();start();testEcho=0;tick(200);testEcho=10*58;tick(200);tick(80);off();
 assert(Serial.out.str().find("ERR obstacle_too_close")!=std::string::npos);
 // Recovery never bypasses the heartbeat watchdog.
 reset();start();testEcho=0;tick(200);testMillis+=1500;loop();off();
 assert(Serial.out.str().find("ERR watchdog")!=std::string::npos);
 // Older nine-field settings fail closed, without enabling exploration.
 reset();command("NAVCFG 10 30 45 20 45 8 80 200 15");off();assert(!navigationConfigured);
 reset();command("NAVCFG 10 30 45 20 45 8 80 200 15 200 2");off();assert(!navigationConfigured);
 // Too-close front obstacle cannot initiate a turn; bounded search stops if boxed in.
 reset();start();testEcho=10*58;tick(200);off();assert(Serial.out.str()=="ERR obstacle_too_close\n");
 reset();start();testEcho=25*58;
 for(int i=0;i<8;i++){tick(200);assert(moving && navigationPhase==NAV_TURN);completeSegment();}
 tick(200);off();assert(Serial.out.str().find("ERR navigation_blocked")!=std::string::npos);
 // A 30-45 cm reading does not start forward motion; it requests a search turn.
 reset();start();testEcho=40*58;tick(200);assert(navigationPhase==NAV_TURN);command("STOP");off();
 // Duration and watchdog guards apply even between segments or during turning.
 reset();configure();command("EXPLORE 1 30");tick(200);Serial.clear();tick(800);off();
 assert(Serial.out.str()=="OK EXPLORE 1\n");
 reset();start();testMillis+=1500;loop();off();assert(Serial.out.str()=="ERR watchdog\n");
 reset();start();testEcho=25*58;tick(200);Serial.clear();testMillis+=1500;loop();off();
 assert(Serial.out.str()=="ERR watchdog\n");
 // Genuine encoder stalls retain their full report and cannot restart exploration.
 reset();start();tick(200);Serial.clear();tick(1500);off();
 assert(Serial.out.str().find("ERR stall slots=1,2")==0);tick(200);off();
 // Movement completion still requires both wheels; the bounded tolerance is unchanged.
 reset();command("DRIVE 40 30");leftMotor.position=-707;rightMotor.position=705;loop();off();
 assert(Serial.out.str()=="OK DRIVE 40\n");
 reset();command("DRIVE 40 30");leftMotor.position=-707;rightMotor.position=704;loop();
 Serial.clear();tick(1500);off();assert(Serial.out.str().find("ERR stall slots=2")==0);
 // Other motor/config commands cannot hijack an active exploration in its waiting phase.
 reset();start();command("DRIVE 10 30");off();assert(Serial.out.str()=="ERR busy\n");
 reset();start();command("NAVCFG 10 30 45 20 45 8 80 200 15 800 2");off();
 assert(Serial.out.str()=="ERR busy\n");
}
"""

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', default=shutil.which('clang++') or shutil.which('g++'))
    parser.add_argument('--sysroot')
    args=parser.parse_args()
    if not args.compiler:
        parser.error('A C++17 compiler is required')
    sketch=Path(__file__).resolve().parents[1]/'firmware/rover/rover.ino'
    with tempfile.TemporaryDirectory(prefix='heartbeat-firmware-') as directory:
        tmp=Path(directory)
        (tmp/'MeAuriga.h').write_text(ARDUINO)
        (tmp/'EEPROM.h').write_text(EEPROM)
        (tmp/'util').mkdir()
        (tmp/'util/atomic.h').write_text('#define ATOMIC_RESTORESTATE 0\n#define ATOMIC_BLOCK(x) for(bool once=true;once;once=false)\n')
        (tmp/'check.cpp').write_text('#include '+json.dumps(str(sketch))+'\n'+CHECKS)
        command=[args.compiler,'-std=c++17','-fsanitize=address,undefined','-I',str(tmp)]
        if args.sysroot:
            command+=['-isysroot',args.sysroot]
        subprocess.run(command+[str(tmp/'check.cpp'),'-o',str(tmp/'check')],check=True)
        subprocess.run([str(tmp/'check')],check=True)
    print('Actual rover firmware: sensor, navigation, STOP, watchdog, stall and completion checks passed')

if __name__=='__main__':
    main()
