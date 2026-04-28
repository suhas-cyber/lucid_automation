#pragma once
#include <functional>

struct pwm_threshold {
  float min, max;
  std::function<float(float)> func;
  char message[16];
};

/* Input resistor network
Rp: Pullup resistor
Ri: Input resistor
Rg: Pulldown resistor
Vp: Reference voltage
Vi: Input voltage from pilot signal
Vg: Ground voltage (usually zero)

(Vp)---[ Rp ]---+
                |
(Vi)---[ Ri ]---+---(Vo)
                |
(Vg)---[ Rg ]---+

*/

#define Rp_OHMS (39000.0f)
#define Rg_OHMS (47000.0f)
#define Ri_OHMS (300000.0f)
#define Vg_VOLTS (0.00f)

// V2 Pilot Dingus
#define Vp_VOLTS (2.50f)

// V1 Pilot Dingus
//#define Vp_VOLTS (2.048f)

// TODO: These constants are probably calculated at compile time, but it's not guaranteed.
// Is it worth doing the calculation here, or externally and then typing in the result?
// Another option is to calculate once at run time and store in a variable.
#define Vo_numerator(Vi) (Vi * Rp_OHMS * Rg_OHMS + Vp_VOLTS * Ri_OHMS * Rg_OHMS + Vg_VOLTS * Ri_OHMS * Rp_OHMS)
#define Vo_denominator (Rp_OHMS * Rg_OHMS + Ri_OHMS * Rg_OHMS + Ri_OHMS * Rp_OHMS)
#define Vo_VOLTS(Vi) (Vo_numerator(Vi) / Vo_denominator)
#define Vo_OFFSET_VOLTS (Vo_VOLTS(0))
#define Vo_INPUT_GAIN (1 / (Vo_VOLTS(1) - Vo_OFFSET_VOLTS))

float filter_f(float old_v, float new_v, float factor);
float vin_from_adc(int val);
struct pwm_threshold * get_threshold_ptr(int state);
void set_pilot_mode(struct sim_state *state, enum pilot_mode mode);
void isr_pilot_edge(uint gp, uint32_t event_mask);
void update_pilot_state(struct sim_state *state);
void update_advertised_current(struct sim_state *state);
void update_calculated_values(struct sim_state *state);