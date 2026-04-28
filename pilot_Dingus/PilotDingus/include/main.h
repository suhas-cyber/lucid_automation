#pragma once
#include <Arduino.h>

enum pilot_state {
  PILOT_STATE_UNKNOWN,
  PILOT_STATE_A,
  PILOT_STATE_B1,
  PILOT_STATE_B2,
  PILOT_STATE_C,
  PILOT_STATE_D,
  PILOT_STATE_E,
  PILOT_STATE_F,
  PILOT_STATE_MAX
};

enum pilot_mode {
  PILOT_MODE_HIZ,
  PILOT_MODE_CONNECT,
  PILOT_MODE_CHARGE,
  PILOT_MODE_VENT,
  PILOT_MODE_MAX
};

struct pilot_cal {
  float neg12 = -12.0;
  float pos12 = 12.0;
};

struct sim_state {
  volatile uint64_t pilot_last_edge_ts = 0;
  volatile uint32_t pilot_rise_ts = 0;
  volatile uint32_t pilot_fall_ts = 0;
  volatile uint32_t pilot_high_duration = 0;
  volatile uint32_t pilot_low_duration = 0;
  volatile int vhi = 0;
  volatile int vlo = 0;
  volatile uint32_t pilot_state_changes = 0;
  float duty;
  float frequency_hz;
  bool pwm_detected = false;
  enum pilot_mode pilot_mode = PILOT_MODE_HIZ;
  enum pilot_state pilot_state = PILOT_STATE_UNKNOWN;
  int display_page = 0;
  float advertised_current = 0.0;
  int advertised_state = -1;
  pilot_cal calibration;
  bool logging = false;
};

#define arr_len(a) (sizeof(a) / sizeof(*(a)))

struct sim_state * get_sim_state(void);