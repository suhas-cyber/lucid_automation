#include "main.h"
#include "hardware/adc.h"
#include "config.h"
#include "pilot.h"


float filter_f(float old_v, float new_v, float factor) {
  if (old_v == 0) {
    return new_v;
  } else {
    return ((1.0 - factor) * old_v) + (factor * new_v);
  }
}

bool check_value_with_tolerance(float actual_val, float expected_val, float tolerance) {
  return (fabs(expected_val - actual_val) < tolerance);
}

pwm_threshold pwm_threshold_map[] = {
  {0.00, 0.03, [](float d){return 0.0;}, "Duty Too Low"},
  {0.03, 0.07, [](float d){return 0.0;}, "Comm Req"},
  {0.07, 0.08, [](float d){return 0.0;}, "Error"},
  {0.08, 0.10, [](float d){return 6.0;}, "Minimum"},
  {0.10, 0.85, [](float d){return d*100 * 0.6;}, "Range 1"},
  {0.85, 0.96, [](float d){return (d*100 - 64) * 2.5;}, "Range 2"},
  {0.96, 0.97, [](float d){return 80.0;}, "Maximum"},
  {0.97, 1E6, [](float d){return 0.0;}, "Duty Too High"},
};

struct pwm_threshold * get_threshold_ptr(int state) {
    return &pwm_threshold_map[state];
}




float adc_to_raw_voltage(int val) {
  return (Vp_VOLTS * val) / 4096.0;
}

float mapF(float x, float in_min, float in_max, float out_min, float out_max)
{
  return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min;
}

float vin_from_adc(int val) {
  float raw_volts = (adc_to_raw_voltage(val) - Vo_OFFSET_VOLTS) * Vo_INPUT_GAIN;
  //return mapF(raw_volts, sim_state.calibration.neg12, sim_state.calibration.pos12, -12.0, 12.0);
  return raw_volts;
}

void update_advertised_current(struct sim_state *state) {
  for (int i; i < arr_len(pwm_threshold_map); i++) {
    if (state->duty > pwm_threshold_map[i].min && state->duty <= pwm_threshold_map[i].max) {
      float temp_current = pwm_threshold_map[i].func(state->duty);
      state->advertised_current = filter_f(state->advertised_current, temp_current, 0.5);
      state->advertised_state = i;
      return;
    }
  }
  // Duty doesn't match anything (weird) so go back to unknown state
  state->advertised_current = 0.0;
  state->advertised_state = -1;
}

void update_pilot_state(struct sim_state *state) {

  float temp_voltage = vin_from_adc(state->vhi);
  enum pilot_state new_state = PILOT_STATE_UNKNOWN;

  if (state->pwm_detected) {
    // PWM is running, possible states B, C, or D
    if (check_value_with_tolerance(temp_voltage, 9.0, VOLTAGE_TOLERANCE_V)) {
      // State B2, vehicle detected with PWM
      new_state = PILOT_STATE_B2;
    } else if (check_value_with_tolerance(temp_voltage, 6.0, VOLTAGE_TOLERANCE_V)) {
      // State C, charging
      new_state = PILOT_STATE_C;
    } else if (check_value_with_tolerance(temp_voltage, 3.0, VOLTAGE_TOLERANCE_V)) {
      // State D, vent
      new_state = PILOT_STATE_D;
    } else {
      new_state = PILOT_STATE_UNKNOWN;
    }
  } else {
    // PWM not detected, possible states A, E, or F
    if (check_value_with_tolerance(temp_voltage, 12.0, 0.5)) {
      // State A, standby
      new_state = PILOT_STATE_A;
    } else if (check_value_with_tolerance(temp_voltage, 9.0, VOLTAGE_TOLERANCE_V)) {
      // State B1, vehicle detected without PWM
      new_state = PILOT_STATE_B1;
    } else if (check_value_with_tolerance(temp_voltage, 0.0, 1.5)) {
      // State E, no power
      new_state = PILOT_STATE_E;
    } else if (check_value_with_tolerance(temp_voltage, -12.0, 0.5)) {
      // State F, error
      new_state = PILOT_STATE_F;
    } else {
      new_state = PILOT_STATE_UNKNOWN;
    }
  }

  if (new_state != state->pilot_state) {
    state->pilot_state = new_state;
    if (state->pilot_state_changes < UINT32_MAX) {
      state->pilot_state_changes++;
    }
  }

}

void set_pilot_mode(struct sim_state *state, enum pilot_mode mode) {

  switch(mode) {
    case PILOT_MODE_HIZ:
      digitalWrite(MODE0, LOW);
      digitalWrite(MODE1, LOW);
      digitalWrite(MODE2, LOW);
      break;

    case PILOT_MODE_CONNECT:
      digitalWrite(MODE0, HIGH);
      digitalWrite(MODE1, LOW);
      digitalWrite(MODE2, LOW);
      break;

    case PILOT_MODE_CHARGE:
      digitalWrite(MODE0, HIGH);
      digitalWrite(MODE1, HIGH);
      digitalWrite(MODE2, LOW);
      break;

    case PILOT_MODE_VENT:
      digitalWrite(MODE0, HIGH);
      digitalWrite(MODE1, HIGH);
      digitalWrite(MODE2, HIGH);
      break;

    default:
      return;
      break;
  }

  state->pilot_mode = mode;
}

void isr_pilot_edge(uint gp, uint32_t event_mask) {

  if (gp == PILOT_EDGE) {
    struct sim_state * state = get_sim_state();
    uint64_t temp_ts = time_us_64();
    state->pilot_last_edge_ts = temp_ts;

    if (event_mask & GPIO_IRQ_EDGE_RISE) {
      state->pilot_rise_ts = (uint32_t)temp_ts;
      state->pilot_low_duration = state->pilot_rise_ts - state->pilot_fall_ts + 2;
      busy_wait_us_32(15);
      state->vhi = adc_read();
    } else if (event_mask & GPIO_IRQ_EDGE_FALL) {
      state->pilot_fall_ts = (uint32_t)temp_ts;
      state->pilot_high_duration = state->pilot_fall_ts - state->pilot_rise_ts - 2;
      busy_wait_us_32(15);
      state->vlo = adc_read();
    }
  }
}

void update_calculated_values(struct sim_state *state) {
  
  // Detect whether pilot PWM is running
  if (time_us_64() - state->pilot_last_edge_ts > PILOT_PWM_TIMEOUT_US) {
    state->pwm_detected = false;
  } else {
    state->pwm_detected = true;
  }

  // Calculate duty cycle and frequency if possible
  if (state->pwm_detected) {
    uint32_t temp_total_pilot_duration = state->pilot_low_duration + state->pilot_high_duration;
    if (temp_total_pilot_duration > 0) {
      // Guard division by zero
      float temp_duty = (float)state->pilot_high_duration / temp_total_pilot_duration;
      state->duty = filter_f(state->duty, temp_duty, 0.4);
      float temp_frequency = (float)SECOND_US / temp_total_pilot_duration;
      state->frequency_hz = filter_f(state->frequency_hz, temp_frequency, 0.4);
    }
  } else {
    // No PWM, so no frequency and either 100% or 0% duty cycle
    int temp_adc = adc_read();
    state->vhi = temp_adc;
    state->vlo = temp_adc;
    state->frequency_hz = 0;
    if (digitalRead(PILOT_EDGE) == HIGH) {
      state->duty = 1.0;
    } else {
      state->duty = 0.0;
    }
  }

  // Determine the EVSE state, if possible
  update_pilot_state(state);
  update_advertised_current(state);
}