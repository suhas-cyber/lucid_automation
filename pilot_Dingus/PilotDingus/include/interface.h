#pragma once
#include "main.h"

void btn0_pressed(struct sim_state *state);
void btn1_pressed(struct sim_state *state);
void btn2_pressed(struct sim_state *state);

void btn0_held(struct sim_state *state);
void btn1_held(struct sim_state *state);
void btn2_held(struct sim_state *state);

void update_display(struct sim_state *state);
void emit_json_packet(struct sim_state *state);
void setup_display(void);

// ADD THIS — getter for pilot state letter (fixes main.cpp compile error)
const char* get_pilot_state_name(enum pilot_state state);