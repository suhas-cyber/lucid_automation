enum button_status {
  BUTTON_IDLE,
  BUTTON_DEBOUNCING,
  BUTTON_PRESSED,
  BUTTON_HELD,
  BUTTON_DONE,
};

struct button_state {
  const uint8_t pin;
  uint32_t ts;
  button_status state;
  void (*cb_pressed)(struct sim_state *state);
  void (*cb_held)(struct sim_state *state);
};

void check_buttons(uint32_t current_ms, struct sim_state *state);