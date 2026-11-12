/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Verified, bounded SII access using the workspace's SOEM raw-datagram API.
 * No PDO mapping, slave state transitions or actuator commands are issued.
 * EEPROM addresses are WORD addresses; files and diagnostics use byte offsets.
 */
#define _POSIX_C_SOURCE 200809L
#include "ethercat.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>

#define MAX_IMAGE 131072u
#define WORD_TRIES 8
#define OP_TIMEOUT_US 300000

static uint16_t station;
static uint16_t last_status;
static int last_wkc;
static unsigned retry_count;

static uint64_t monotonic_us(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000u + (uint64_t)t.tv_nsec / 1000u;
}

static void pause_us(unsigned usec) {
    struct timespec t = {(time_t)(usec / 1000000u), (long)(usec % 1000000u) * 1000L};
    while (nanosleep(&t, &t) && errno == EINTR) {}
}

static uint16_t little16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

static int read_register(uint16_t reg, uint16_t size, void *data) {
    last_wkc = ec_APRD(station, reg, size, data, EC_TIMEOUTRET3);
    return last_wkc > 0;
}

static int write_register(uint16_t reg, uint16_t size, void *data) {
    last_wkc = ec_APWR(station, reg, size, data, EC_TIMEOUTRET3);
    return last_wkc > 0;
}

static int ready(uint16_t *status) {
    uint64_t deadline = monotonic_us() + OP_TIMEOUT_US;
    do {
        uint8_t data[2] = {0, 0};
        if (read_register(ECT_REG_EEPSTAT, 2, data)) {
            *status = little16(data);
            last_status = *status;
            if (!(*status & EC_ESTAT_BUSY)) return 1;
        }
        pause_us(1000);
    } while (monotonic_us() < deadline);
    fprintf(stderr, "EEPROM wait timeout: wkc=%d last_status=0x%04x\n", last_wkc, last_status);
    return 0; /* A zero WKC is never interpreted as a successful idle device. */
}

static int clear_errors(void) {
    uint16_t status;
    if (!ready(&status)) return 0;
    if (status & EC_ESTAT_EMASK) {
        uint8_t nop[2] = {0, 0};
        if (!write_register(ECT_REG_EEPCTL, 2, nop)) return 0;
        pause_us(1000);
        if (!ready(&status) || (status & EC_ESTAT_EMASK)) return 0;
    }
    return 1;
}

static int command(uint16_t type, uint16_t word) {
    uint8_t data[6] = {0};
    put16(data, type);
    put16(data + 2, word);
    return write_register(ECT_REG_EEPCTL, sizeof(data), data);
}

static int read_chunk(uint16_t word, uint8_t data[8], unsigned *length) {
    uint16_t status;
    if (!clear_errors() || !command(EC_ECMD_READ, word)) return 0;
    pause_us(1000);
    if (!ready(&status) || (status & EC_ESTAT_EMASK)) return 0;
    *length = (status & EC_ESTAT_R64) ? 8u : 4u;
    return read_register(ECT_REG_EEPDAT, (uint16_t)*length, data);
}

static int read_word(uint16_t address, uint16_t *value) {
    uint8_t data[8];
    unsigned count;
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        if (read_chunk(address, data, &count)) {
            *value = little16(data);
            return 1;
        }
        pause_us(5000);
    }
    return 0;
}

static int read_image(uint8_t *data, unsigned size) {
    for (unsigned i = 0; i < size;) {
        uint8_t block[8];
        unsigned count = 0;
        int ok = 0;
        for (unsigned attempt = 0; attempt < 3; ++attempt) {
            if (read_chunk((uint16_t)(i / 2), block, &count)) { ok = 1; break; }
            pause_us(5000);
        }
        if (!ok) {
            fprintf(stderr, "EEPROM read failed: byte=0x%04x status=0x%04x\n", i, last_status);
            return 0;
        }
        if (count > size - i) count = size - i;
        memcpy(data + i, block, count);
        i += count;
    }
    return 1;
}

static int stable_image(uint8_t *data, unsigned size) {
    uint8_t *second = malloc(size);
    if (!second) return 0;
    int ok = 0;
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        if (read_image(data, size) && read_image(second, size) && !memcmp(data, second, size)) {
            ok = 1; break;
        }
        fprintf(stderr, "EEPROM read unstable, retry %u/3\n", attempt + 1);
        pause_us(10000);
    }
    free(second);
    return ok;
}

static int write_verified_word(uint16_t address, uint16_t expected) {
    uint16_t actual = 0, status = 0;
    for (unsigned attempt = 1; attempt <= WORD_TRIES; ++attempt) {
        if (clear_errors()) {
            uint8_t bytes[2]; put16(bytes, expected);
            if (write_register(ECT_REG_EEPDAT, 2, bytes) &&
                command(EC_ECMD_WRITE, address)) {
                /* Do not flood the ESC/serial EEPROM while its write is settling. */
                pause_us(5000);
                if (ready(&status) && !(status & EC_ESTAT_EMASK) &&
                    read_word(address, &actual) && actual == expected) return 1;
            }
        }
        ++retry_count;
        fprintf(stderr, "EEPROM word retry: byte=0x%04x expected=0x%04x actual=0x%04x status=0x%04x wkc=%d attempt=%u/%u\n",
                (unsigned)address * 2, expected, actual, last_status, last_wkc, attempt, WORD_TRIES);
        pause_us(5000u * attempt);
    }
    return 0;
}

static int connect_bus(const char *interface, int slave) {
    if (!ec_init(interface)) { fprintf(stderr, "Cannot open NIC %s (raw socket/root required)\n", interface); return 0; }
    uint16_t value = 0;
    int count = ec_BRD(0, ECT_REG_TYPE, sizeof(value), &value, EC_TIMEOUTSAFE);
    if (count < slave) { fprintf(stderr, "EtherCAT slaves found=%d, requested=%d\n", count, slave); return 0; }
    printf("EtherCAT slaves found=%d; using chain position %d on %s\n", count, slave, interface);
    station = (uint16_t)(1 - slave);
    uint8_t owner = 2;
    if (!write_register(ECT_REG_EEPCFG, 1, &owner)) return 0;
    owner = 0;
    if (!write_register(ECT_REG_EEPCFG, 1, &owner)) return 0;
    pause_us(5000);
    return clear_errors();
}

static int save_backup(const char *path) {
    uint8_t header[128];
    if (!stable_image(header, sizeof(header))) return 0;
    unsigned size = ((unsigned)little16(header + 124) + 1u) * 128u;
    if (size < 128 || size > MAX_IMAGE) { fprintf(stderr, "Invalid EEPROM capacity %u\n", size); return 0; }
    uint8_t *data = malloc(size);
    if (!data) return 0;
    int ok = stable_image(data, size);
    if (ok) {
        FILE *out = fopen(path, "wb");
        if (!out) ok = 0;
        else {
            ok = fwrite(data, 1, size, out) == size;
            if (fclose(out)) ok = 0;
        }
    }
    free(data);
    if (ok) printf("EEPROM_READ_VERIFIED bytes=%u (two matching reads)\n", size);
    return ok;
}

static int program(const char *path) {
    FILE *in = fopen(path, "rb");
    if (!in) return 0;
    if (fseek(in, 0, SEEK_END)) { fclose(in); return 0; }
    long count = ftell(in);
    rewind(in);
    if (count < 128 || count > MAX_IMAGE || count % 2) { fclose(in); return 0; }
    unsigned size = (unsigned)count;
    uint8_t *expected = malloc(size), *actual = malloc(size);
    if (!expected || !actual) { free(expected); free(actual); fclose(in); return 0; }
    int ok = fread(expected, 1, size, in) == size;
    fclose(in);
    if (!ok || !stable_image(actual, size)) { free(expected); free(actual); return 0; }
    unsigned changed = 0;
    for (unsigned i = 0; i < size; i += 2) if (memcmp(expected + i, actual + i, 2)) ++changed;
    printf("EEPROM diff: %u mismatching 16-bit words; matching words will not be rewritten\n", changed);
    unsigned completed = 0;
    for (unsigned i = 0; i < size; i += 2) {
        if (!memcmp(expected + i, actual + i, 2)) continue;
        if (!write_verified_word((uint16_t)(i / 2), little16(expected + i))) {
            fprintf(stderr, "EEPROM_WRITE_FAILED byte=0x%04x; stopped without claiming success\n", i);
            free(expected); free(actual); return 0;
        }
        ++completed;
        if (completed % 32 == 0 || completed == changed) printf("EEPROM progress: %u/%u repaired words\n", completed, changed);
    }
    ok = stable_image(actual, size) && !memcmp(actual, expected, size);
    if (ok) printf("EEPROM_WRITE_VERIFIED bytes=%u changed_words=%u retries=%u\n", size, changed, retry_count);
    else fprintf(stderr, "EEPROM full-image verification failed\n");
    free(expected); free(actual);
    return ok;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc != 5 || (strcmp(argv[3], "-r") && strcmp(argv[3], "-w"))) {
        fprintf(stderr, "Usage: eeprom_verified NIC SLAVE -r|-w FILE\n"); return 2;
    }
    char *end = NULL;
    long slave = strtol(argv[2], &end, 10);
    if (!end || *end || slave < 1 || slave > 255) return 2;
    if (!connect_bus(argv[1], (int)slave)) { ec_close(); return 3; }
    int ok = !strcmp(argv[3], "-r") ? save_backup(argv[4]) : program(argv[4]);
    ec_close();
    return ok ? 0 : 4;
}
