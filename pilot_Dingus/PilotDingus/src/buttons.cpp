#include "main.h"
#include "config.h"
#include "interface.h"
#include "buttons.h"

button_state button_states[3] = {
  {BTN0, 0, BUTTON_IDLE, btn0_pressed, NULL},
  {BTN1, 0, BUTTON_IDLE, btn1_pressed, NULL},
  {BTN2, 0, BUTTON_IDLE, btn2_pressed, NULL},
};

void check_buttons(uint32_t current_ms, struct sim_state *state) {

  for (int i = 0; i < 3; i++) {

    if (!gpio_get(button_states[i].pin)) {
      if (button_states[i].state == BUTTON_IDLE) {
        button_states[i].ts = current_ms;
        button_states[i].state = BUTTON_DEBOUNCING;
      } else if (button_states[i].state == BUTTON_DEBOUNCING) {
        if (current_ms - button_states[i].ts > 35) {
          // Button has been pressed, but don't do anything until released
          // because this might be the start of a button hold interaction
          button_states[i].state = BUTTON_PRESSED;
        }
      } else if (button_states[i].state == BUTTON_PRESSED) {
        if (current_ms - button_states[i].ts > 700) {
          // Button is being held down
          if (button_states[i].cb_held) {
            button_states[i].cb_held(state);
          }
          button_states[i].state = BUTTON_HELD;
        }
      }
    } else {
      if (button_states[i].state == BUTTON_PRESSED) {
        // Button released from short press (click)
        if (button_states[i].cb_pressed) {
          button_states[i].cb_pressed(state);
        }
        button_states[i].state = BUTTON_IDLE;
      } else if (button_states[i].state == BUTTON_HELD) {
        // Button released from being held down
        button_states[i].state = BUTTON_IDLE;
      }
    }

  }
}