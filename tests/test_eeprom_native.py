"""Compile and execute the real C algorithm against an in-memory ESC simulator.

The fixture does not create raw sockets or communicate with real hardware.
It models ACKed writes that fail to change memory, the original failure mode.
"""
from pathlib import Path
import os
import shutil
import subprocess

import pytest


HEADER = r'''
#include <stdint.h>
#define ECT_REG_TYPE 0
#define ECT_REG_EEPCFG 0x500
#define ECT_REG_EEPCTL 0x502
#define ECT_REG_EEPSTAT 0x502
#define ECT_REG_EEPDAT 0x508
#define EC_TIMEOUTRET3 6000
#define EC_TIMEOUTSAFE 20000
#define EC_ECMD_READ 0x100
#define EC_ECMD_WRITE 0x201
#define EC_ESTAT_R64 0x40
#define EC_ESTAT_BUSY 0x8000
#define EC_ESTAT_EMASK 0x7800
int ec_init(const char *);
void ec_close(void);
int ec_BRD(uint16_t,uint16_t,uint16_t,void *,int);
int ec_APRD(uint16_t,uint16_t,uint16_t,void *,int);
int ec_APWR(uint16_t,uint16_t,uint16_t,const void *,int);
'''

SIMULATOR = r'''
#include "ethercat.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static unsigned char memory[131072];
static unsigned size, address, staged, dropped;
static unsigned get16(const unsigned char *p) { return p[0] | (p[1]<<8); }
int ec_init(const char *nic) {
    (void)nic;
    const char *path=getenv("SIM_EEPROM");
    if(!path) return 0;
    FILE *file=fopen(path,"rb");if(!file)return 0;
    size=fread(memory,1,sizeof(memory),file);fclose(file);return size>=128;
}
void ec_close(void) {
    if(!size)return;
    FILE *file=fopen(getenv("SIM_EEPROM"),"wb");
    if(file){fwrite(memory,1,size,file);fclose(file);}
}
int ec_BRD(uint16_t a,uint16_t b,uint16_t c,void *p,int t) {
    (void)a;(void)b;(void)c;(void)p;(void)t;return 1;
}
int ec_APRD(uint16_t station,uint16_t reg,uint16_t length,void *ptr,int timeout) {
    (void)station;(void)timeout;
    unsigned char *data=ptr;
    if(reg==ECT_REG_EEPSTAT) {
        if(getenv("SIM_LOST_STATUS"))return 0;
        data[0]=0x40;data[1]=0;return 1;
    }
    if(reg==ECT_REG_EEPDAT){
        for(unsigned i=0;i<length;i++)data[i]=memory[(address*2+i)%size];return 1;
    }
    memset(ptr,0,length);return 1;
}
int ec_APWR(uint16_t station,uint16_t reg,uint16_t length,const void *ptr,int timeout) {
    (void)station;(void)timeout;
    const unsigned char *data=ptr;
    if(reg==ECT_REG_EEPDAT){staged=get16(data);return 1;}
    if(reg==ECT_REG_EEPCTL && length==6){
        address=get16(data+2);
        if(get16(data)==EC_ECMD_WRITE){
            FILE *trace=fopen(getenv("SIM_WRITES"),"a");
            if(trace){fprintf(trace,"%u\n",address);fclose(trace);}
            const char *drop=getenv("SIM_DROP_WORD");
            unsigned max=getenv("SIM_DROP_COUNT")?(unsigned)atoi(getenv("SIM_DROP_COUNT")):0;
            if(drop && address==(unsigned)atoi(drop) && dropped++<max)return 1;
            memory[address*2]=(unsigned char)staged;
            memory[address*2+1]=(unsigned char)(staged>>8);
        }
    }
    return 1;
}
'''


@pytest.fixture(scope='module')
def executable(tmp_path_factory):
    if not shutil.which('cc'):
        pytest.skip('C compiler unavailable')
    directory = tmp_path_factory.mktemp('eeprom-native-simulator')
    (directory / 'ethercat.h').write_text(HEADER)
    (directory / 'fake_esc.c').write_text(SIMULATOR)
    native = Path(__file__).resolve().parents[1] / 'ecat_groundstation/eeprom_native.c'
    binary = directory / 'test_eeprom_verified'
    subprocess.run(['cc', '-std=c99', '-O2', '-Wall', '-Wextra', '-I', str(directory),
                    str(native), str(directory / 'fake_esc.c'), '-o', str(binary)], check=True)
    return binary


@pytest.fixture
def device(tmp_path):
    expected = bytearray(b'\xff' * 2048)
    expected[124:126] = (15).to_bytes(2, 'little')
    image = tmp_path / 'large-pdo.bin'
    image.write_bytes(expected)
    memory = tmp_path / 'device.bin'
    memory.write_bytes(expected)
    writes = tmp_path / 'writes.txt'
    env = os.environ.copy()
    env.update(SIM_EEPROM=str(memory), SIM_WRITES=str(writes))
    return expected, image, memory, writes, env


def test_repairs_only_five_words_with_transient_silent_drop(executable, device):
    expected, image, memory, writes, env = device
    partial = bytearray(expected)
    # Reproduces the seven differing byte positions in the user's second readback.
    for offset in (160, 161, 204, 218, 219, 233, 247):
        partial[offset] ^= 1
    memory.write_bytes(partial)
    env.update(SIM_DROP_WORD='80', SIM_DROP_COUNT='3')
    run = subprocess.run([executable, 'fake-nic', '1', '-w', image], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert memory.read_bytes() == bytes(expected)
    assert 'changed_words=5 retries=3' in run.stdout
    addresses = [int(n) for n in writes.read_text().splitlines()]
    assert set(addresses) == {80, 102, 109, 116, 123}
    assert addresses.count(80) == 4


def test_permanent_failure_stops_after_bounded_retries(executable, device):
    expected, image, memory, writes, env = device
    partial = bytearray(expected)
    partial[160] ^= 1
    memory.write_bytes(partial)
    env.update(SIM_DROP_WORD='80', SIM_DROP_COUNT='999')
    run = subprocess.run([executable, 'fake-nic', '1', '-w', image], env=env, capture_output=True, text=True)
    assert run.returncode != 0
    assert 'EEPROM_WRITE_VERIFIED' not in run.stdout
    assert 'EEPROM_WRITE_FAILED byte=0x00a0' in run.stderr
    assert len(writes.read_text().splitlines()) == 8


def test_matching_image_does_not_rewrite_eeprom(executable, device):
    _, image, _, writes, env = device
    run = subprocess.run([executable, 'fake-nic', '1', '-w', image], env=env, capture_output=True, text=True)
    assert run.returncode == 0
    assert 'changed_words=0 retries=0' in run.stdout
    assert not writes.exists()


def test_readback_uses_two_matching_reads_without_writing(executable, device, tmp_path):
    expected, _, memory, writes, env = device
    output = tmp_path / 'readback.bin'
    run = subprocess.run([executable, 'fake-nic', '1', '-r', output], env=env, capture_output=True, text=True)
    assert run.returncode == 0
    assert output.read_bytes() == memory.read_bytes() == bytes(expected)
    assert 'two matching reads' in run.stdout
    assert not writes.exists()


def test_lost_working_counter_is_not_mistaken_for_success(executable, device):
    _, image, _, writes, env = device
    env['SIM_LOST_STATUS'] = '1'
    run = subprocess.run([executable, 'fake-nic', '1', '-w', image], env=env, capture_output=True, text=True)
    assert run.returncode != 0 and not writes.exists()
