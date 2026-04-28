#include <Arduino.h>
#include "hardware/adc.h"
#include <EEPROM.h>
#include "config.h"
#include "main.h"
#include "interface.h"
#include "buttons.h"
#include "pilot.h"

struct sim_state sim_state;

struct sim_state * get_sim_state(void) {
  return &sim_state;
}

bool validate_cal(pilot_cal cal) {
  return ~(cal.neg12 < -20 || cal.neg12 > 9 || cal.pos12 > 20 || cal.pos12 < 9);
}

// Returns mode name matching pilot_mode_map in interface.cpp
const char* get_pilot_mode_name(enum pilot_mode mode) {
  switch(mode) {
    case PILOT_MODE_HIZ:     return "WATCH";
    case PILOT_MODE_CONNECT: return "CONNECT";
    case PILOT_MODE_CHARGE:  return "CHARGE";
    case PILOT_MODE_VENT:    return "VENT";
    default:                 return "UNKNOWN";
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  EEPROM.begin(4096);
  analogReadResolution(12);
  pinMode(BTN0, INPUT_PULLUP);
  pinMode(BTN1, INPUT_PULLUP);
  pinMode(BTN2, INPUT_PULLUP);
  pinMode(MODE0, OUTPUT);
  pinMode(MODE1, OUTPUT);
  pinMode(MODE2, OUTPUT);
  pinMode(PILOT_EDGE, INPUT);

  adc_init();
  adc_select_input(0);
  adc_gpio_init(A0);

  sim_state.calibration.neg12 = -12.0;
  sim_state.calibration.pos12 = 12.0;

  gpio_set_irq_enabled_with_callback(PILOT_EDGE, GPIO_IRQ_EDGE_RISE + GPIO_IRQ_EDGE_FALL, true, isr_pilot_edge);
  irq_set_priority(IO_IRQ_BANK0, PICO_HIGHEST_IRQ_PRIORITY);
  set_pilot_mode(&sim_state, PILOT_MODE_HIZ);

  // Boot message so lab cell knows device is ready
  Serial.printf("{\"ack\":\"boot\",\"mode\":\"WATCH\",\"logging\":false,\"ver\":%d}\n", PROTOCOL_VER);
}

void setup1() {
  setup_display();
}

void parse_receive(byte rec) {
  switch (toupper(rec)) {

    case 'A':  // WATCH / HIZ
      set_pilot_mode(&sim_state, PILOT_MODE_HIZ);
      Serial.printf("{\"ack\":\"mode_set\",\"mode\":\"WATCH\"}\n");
      break;

    case 'B':  // CONNECT
      set_pilot_mode(&sim_state, PILOT_MODE_CONNECT);
      Serial.printf("{\"ack\":\"mode_set\",\"mode\":\"CONNECT\"}\n");
      break;

    case 'C':  // CHARGE
      set_pilot_mode(&sim_state, PILOT_MODE_CHARGE);
      Serial.printf("{\"ack\":\"mode_set\",\"mode\":\"CHARGE\"}\n");
      break;

    case 'D':  // VENT
      set_pilot_mode(&sim_state, PILOT_MODE_VENT);
      Serial.printf("{\"ack\":\"mode_set\",\"mode\":\"VENT\"}\n");
      break;

    case 'Z':  // Reset state change counter
      sim_state.pilot_state_changes = 0;
      Serial.printf("{\"ack\":\"counter_reset\"}\n");
      break;

    case 'L':  // Toggle logging
      sim_state.logging = !sim_state.logging;
      Serial.printf("{\"ack\":\"logging\",\"enabled\":%s}\n",
                    sim_state.logging ? "true" : "false");
      break;

    case 'R':  // Single reading
      update_calculated_values(&sim_state);
      emit_json_packet(&sim_state);
      break;

    case 'S':  // Status query - mode + pilot state + counters
      Serial.printf("{\"ack\":\"status\",\"mode\":\"%s\",\"pilot_state\":\"%s\","
                    "\"state_changes\":%d,\"logging\":%s,\"ver\":%d}\n",
                    get_pilot_mode_name(sim_state.pilot_mode),
                    get_pilot_state_name(sim_state.pilot_state),
                    sim_state.pilot_state_changes,
                    sim_state.logging ? "true" : "false",
                    PROTOCOL_VER);
      break;
  }
}

void loop() {
  unsigned long current_time_ms = millis();
  static unsigned long update_timer_ms = 0;
  static unsigned long button_timer_ms = 0;

  if (current_time_ms - update_timer_ms > UPDATE_INTERVAL_MS) {
    update_timer_ms = current_time_ms;
    update_calculated_values(&sim_state);
    if (sim_state.logging) {
      emit_json_packet(&sim_state);
    }
  }

  if (current_time_ms - button_timer_ms > 5) {
    button_timer_ms = current_time_ms;
    check_buttons(current_time_ms, &sim_state);
  }

  while (Serial.available()) {
    byte rec = Serial.read();
    parse_receive(rec);
  }
}

void loop1() {
  update_display(&sim_state);
  delay(50);
}