#include "main.h"
#include "config.h"
#include "interface.h"
#include "pilot.h"

#include <Wire.h>
#include <U8g2lib.h>
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0);

struct pilot_glyph_map {
  char letter[3];
  int glyph;
} const pilot_glyph_map[] = {
  [PILOT_STATE_UNKNOWN] = {"U", 240},
  [PILOT_STATE_A] =       {"A", 223},
  [PILOT_STATE_B1] =      {"B1", 235},
  [PILOT_STATE_B2] =      {"B2", 235},
  [PILOT_STATE_C] =       {"C", 170},
  [PILOT_STATE_D] =       {"D", 205},
  [PILOT_STATE_E] =       {"E", 283},
  [PILOT_STATE_F] =       {"F", 280},
};

const char *pilot_mode_map[] = {
  [PILOT_MODE_HIZ] = {"WATCH"},
  [PILOT_MODE_CONNECT] = {"CONNECT"},
  [PILOT_MODE_CHARGE] = {"CHARGE"},
  [PILOT_MODE_VENT] = {"VENT"},
};

// Replace ONLY this function, everything else stays identical:

void emit_json_packet(struct sim_state *state) {
  char outbuf[256] = {0};
  char* bufptr = outbuf;
  bufptr += sprintf(bufptr, "{ \"ver\": %d, ", PROTOCOL_VER);
  bufptr += sprintf(bufptr, "\"mode\": \"%s\", ", pilot_mode_map[state->pilot_mode]); // NEW
  bufptr += sprintf(bufptr, "\"state\": \"%s\", \"state_changes\": %d, ",pilot_glyph_map[state->pilot_state].letter,state->pilot_state_changes);
  bufptr += sprintf(bufptr, "\"low_v\": %0.2f, \"high_v\": %0.2f, ",vin_from_adc(state->vlo), vin_from_adc(state->vhi));
  bufptr += sprintf(bufptr, "\"duty\": %0.2f, \"frequency\": %0.2f, ",state->duty, state->frequency_hz);
  bufptr += sprintf(bufptr, "\"adv_current\": %0.2f }", state->advertised_current);
  Serial.println(outbuf);
}

void display_page1(struct sim_state *state) {
  u8g2.clearBuffer();
  char strbuf[32] = {0};
  u8g2.setFont(u8g2_font_open_iconic_all_1x_t);
  u8g2.drawGlyph(18,14, pilot_glyph_map[state->pilot_state].glyph);
  u8g2.setFont(u8g2_font_profont17_tf);
  sprintf(strbuf, "%s", pilot_glyph_map[state->pilot_state].letter);
  u8g2.drawStr(0,15,strbuf);
  sprintf(strbuf, "%+6.2f V", vin_from_adc(state->vhi));
  u8g2.drawStr(45,15,strbuf);

  if (state->pwm_detected) {
    sprintf(strbuf, "%2.0f%% %04d Hz", state->duty*100, (int)state->frequency_hz);
    u8g2.drawStr(0,32,strbuf);
  } else {
    sprintf(strbuf, "-- %%   ---- Hz");
    u8g2.drawStr(0,32,strbuf);
  }
  sprintf(strbuf, "advA: %5.1f A", state->advertised_current);
  u8g2.drawStr(0,49,strbuf);
  sprintf(strbuf, "mode: %s", pilot_mode_map[state->pilot_mode]);
  u8g2.drawStr(0,63,strbuf);

  u8g2.sendBuffer();
}

void display_page2(struct sim_state *state) {
  u8g2.clearBuffer();
  char strbuf[32] = {0};
  u8g2.setFont(u8g2_font_inb53_mf);
  sprintf(strbuf, "%s", pilot_glyph_map[state->pilot_state].letter);
  u8g2.drawStr(0,63,strbuf);
  u8g2.setFont(u8g2_font_open_iconic_all_6x_t);
  u8g2.drawGlyph(64,63, pilot_glyph_map[state->pilot_state].glyph);
  u8g2.sendBuffer();
}

void display_page3(struct sim_state *state) {
  u8g2.clearBuffer();
  char strbuf[32] = {0};
  u8g2.setFont(u8g2_font_profont29_tr);
  sprintf(strbuf, "%5.1f A", state->advertised_current);
  u8g2.drawStr(0,31,strbuf);
  u8g2.setFont(u8g2_font_profont17_tf);
  if (state->pilot_state == PILOT_STATE_E) {
    sprintf(strbuf, "No Signal");
  } else if (state->advertised_state >= 0) {
    sprintf(strbuf, "%s", get_threshold_ptr(state->advertised_state)->message);
  } else {
    sprintf(strbuf, "Not Charging");
  }
  int offset = (128 - u8g2.getStrWidth(strbuf)) / 2;
  u8g2.drawStr(offset,60,strbuf);
  u8g2.sendBuffer();
}

void (*pages[])(struct sim_state *state) = {
  display_page1,
  display_page2,
  display_page3,
};

void btn0_pressed(struct sim_state *state) {
  static int p = 0;
  if (++p >= arr_len(pages)) {
    p = 0;
  }
  state->display_page = p;
}

void btn0_held(struct sim_state *state) {
  static int p = 0;
  if (++p >= arr_len(pages)) {
    p = 0;
  }
  state->display_page = p;
}

void btn1_pressed(struct sim_state *state) {
  // Decrement pilot mode
  // Not testing VENT
  switch (state->pilot_mode) {
    case PILOT_MODE_CONNECT:
      set_pilot_mode(state, PILOT_MODE_HIZ);
      break;
    case PILOT_MODE_CHARGE:
      set_pilot_mode(state, PILOT_MODE_CONNECT);
      break;
    default:
      break;
  }
}

void btn1_held(struct sim_state *state) {
  static int p = 0;
  if (++p >= arr_len(pages)) {
    p = 0;
  }
  state->display_page = p;
}

void btn2_pressed(struct sim_state *state) {
  // Increment pilot mode
  // Not testing VENT
  switch (state->pilot_mode) {
    case PILOT_MODE_HIZ:
      set_pilot_mode(state, PILOT_MODE_CONNECT);
      break;
    case PILOT_MODE_CONNECT:
      set_pilot_mode(state, PILOT_MODE_CHARGE);
      break;
    default:
      break;
  }
}

void btn2_held(struct sim_state *state) {
  static int p = 0;
  if (++p >= arr_len(pages)) {
    p = 0;
  }
  state->display_page = p;
}

void update_display(struct sim_state *state) {
  pages[state->display_page](state);
}

void setup_display(void) {
  Wire.setSCL(1);
  Wire.setSDA(0);
  u8g2.setI2CAddress(0x78);
  u8g2.begin();
}
// Add this function - returns state letter without exposing the map
const char* get_pilot_state_name(enum pilot_state state) {
  if (state < 0 || state >= PILOT_STATE_MAX) return "U";
  return pilot_glyph_map[state].letter;
}