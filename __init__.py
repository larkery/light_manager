## TODO
## implement interceptor functions to lock + latch
## add service call to lock / latch
## add service call to unlock / unlatch
## ZIGTIMISE
## Merge in on/off stuff?
## Add transitions? Split commands in interceptor?
import asyncio
import logging
_LOGGER = logging.getLogger(__name__)

from .interceptor import setup_service_call_interceptor

import homeassistant.util.dt as dt_util
from homeassistant.helpers.sun import get_astral_location
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_BRIGHTNESS_STEP,
    ATTR_BRIGHTNESS_STEP_PCT,
    ATTR_COLOR_NAME,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    ATTR_COLOR_MODE,
    ATTR_FLASH,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_WHITE,
    ColorMode,
)

from homeassistant.const import (
    ATTR_ENTITY_ID, SERVICE_TURN_ON, SERVICE_TOGGLE, SERVICE_TURN_OFF, STATE_ON, STATE_OFF
)

from collections import defaultdict
from math import tanh

from homeassistant.core import Context

# block name -> settings
lightsets = {}

# light entity -> stuff
managed_lights = {}

@service("light.lock")
def lock(lights = [],
         brightness = None,
         temperature = None):
    """yaml
name: Lock lights to temperature
fields:
    lights:
      required: true
      selector:
        entity:
          filter:
            domain: light
          multiple: true
    brightness:
       required: true
       default: 1
       selector:
         number:
           min: 1
           max: 255
    temperature:
       required: true
       default: 2200
       selector:
         number:
           min: 2200
           max: 5000
    """
    global managed_lights
    lights = zha_expand(lights)
    for light in lights:
        if light in managed_lights:
            managed_lights[light]["lock"] = (brightness, temperature)
            managed_lights[light]["latch"] = False
    update(force = True, transition = 2)

@service("light.unlock")
def unlock(lights = []):
    """yaml
name: Unlock managed lights
fields:
    lights:
      required: true
      selector:
        entity:
          filter:
            domain: light
          multiple: true

    """
    global managed_lights
    lights = zha_expand(lights)
    for light in lights:
        if light in managed_lights:
            managed_lights[light]["lock"] = None
            managed_lights[light]["latch"] = False
    update(force = True)

@service("light.manage")
def manage(lightset=None,
           lights=[], # a list of entity IDs
           brightness_min=150.0,
           brightness_max=255.0,
           brightness_k = 25.0,
           brightness_x = 0.0,
           temperature_min = 2210,
           temperature_max = 4000,
           temperature_k = 22.0,
           temperature_x = 0.05):
    """yaml
name: Manage lights
description: Set lights to solar management
fields:
    lightset:
      required: true
      selector:
        text:
    lights:
      required: true
      selector:
        entity:
          filter:
            domain: light
          multiple: true
    brightness_min:
      required: true
      default: 200
      selector:
        number:
          min: 0
          max: 255
          mode: slider
    brightness_max:
      required: true
      default: 255
      selector:
        number:
          min: 0
          max: 255
          mode: slider
    temperature_min:
       required: true
       default: 2500
       selector:
         number:
           min: 2200
           max: 5000
           mode: slider
    temperature_max:
       required: true
       default: 3500
       selector:
         number:
           min: 2200
           max: 5000
           mode: slider
    brightness_k:
       required: true
       default: 25.0
       selector:
         number:
    brightness_x:
       required: true
       default: 0
       selector:
         number:
    temperature_k:
       required: true
       default: 22.0
       selector:
         number:
    temperature_x:
       required: true
       default: 0.05
       selector:
         number:
    """
    global lightsets, managed_lights
    lights = zha_expand(lights)
    unmanage(lights)
    
    # re-manage them
    for light in lights:
        managed_lights[light] = {
            "lightset": lightset, 
            "lock": None, # or (brightness, temperature)
            "latch": False # latch clears lock on turn off
        }

    lightsets[lightset] = {
        "brightness":(brightness_k, brightness_x,
                      brightness_min, brightness_max),
        "temperature":(temperature_k, temperature_x,
                       temperature_min, temperature_max),
        "state": None
    }

    update(force = True)

@service("light.unmanage")
def unmanage(lights=[], lightset=None):
    """yaml
name: Stop managing lights
fields:
    lightset:
      selector:
        text:
    lights:
      required: true
      selector:
        entity:
          filter:
            domain: light
          multiple: true
    """
    global lightsets, managed_lights
    lights = zha_expand(lights)
    for light in list(lights) + [k for (k,v) in managed_lights.items()
                                 if v["lightset"] == lightset]:
        managed_lights.pop(light, None)

@time_trigger("cron(* * * * *)")
@service("light.update_managed")
def update(now = None, force = False, transition = 0):
    global lightsets, managed_lights, context
    
    times = get_times(hass)
    if now:
        times = list(times)
        times[0] = now
        times = tuple(times)
    set_states = {name:(curve(times, parameters["brightness"]),
                        curve(times, parameters["temperature"]))
                  for (name, parameters) in lightsets.items()}

    changed = False
    for name in set_states:
        if lightsets[name]["state"] != set_states[name]:
            lightsets[name]["state"] = set_states[name]
            changed = True

    if not(changed or force):
        log.warning("No change to the expected states - early exit")
        return
    
    current_states = {}
    for id in state.names(domain = 'light'):
        val = state.get(id)
        if val == 'unavailable': continue
        att = state.getattr(id)
        if val == 'off' and managed_lights.get(id, {}).get("latch", False):
            ## toggle latch for a locked state
            managed_lights[id]["lock"] = None
            managed_lights[id]["latch"] = False
        
        current_states[id] = (att.get(ATTR_BRIGHTNESS, None),
                              att.get(ATTR_COLOR_TEMP_KELVIN, None))
        
    # brightness and temperature should either be whatever
    # is in lock, or whatever the lightset says, or OFF
    # if the light is off. Next we want to reconcile these
    # and then issue the minimal set of zigbee commands
    target_states = {name:(managed_lights[name]["lock"] or
                           set_states[managed_lights[name]["lightset"]])
                     for name in managed_lights
                     if (current_states[name][0] ## is on
                         and (managed_lights[name]["lock"] or # is locked
                              not(managed_lights[name]["latch"]))) # is not latched to whatever values
                     }

    actions = reconcile(current_states, target_states)

    log.warning(f"AIM FOR {target_states} execute {actions}")

    for (entity, (brightness, temperature)) in actions.items():
        # can I use context here??
        if brightness:
            if transition:
                hass.services.async_call(
                    "light", SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: entity,
                     ATTR_BRIGHTNESS: brightness,
                     ATTR_TRANSITION: transition},
                    context=context
                )
                task.sleep(transition+0.5) # ikea
                hass.services.async_call(
                    "light", SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: entity, ATTR_COLOR_TEMP_KELVIN: temperature},
                    context=context
                )
            else:
                hass.services.async_call(
                    "light", SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: entity,
                     ATTR_BRIGHTNESS: brightness,
                     ATTR_COLOR_TEMP_KELVIN: temperature},
                    context=context
                )
        else:
            hass.services.async_call(
                "light", SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID:entity},
                context=context
            )
        task.sleep(0.4)

@pyscript_compile
def reconcile(current_states, target_states):
    (g2l, l2g) = zha_group_map()
    actions = {}
    action_entity = {}

    for (entity, tgt) in sorted(target_states.items(),
                                key = lambda x : -len(g2l.get(x[0], []))):
        for entity in g2l.get(entity, [entity]):
            if current_states.get(entity, None) != tgt:
                actions[entity] = tgt
                action_entity[entity] = entity
            elif entity in actions:
                del actions[entity]
                del action_entity[entity]

    target_states = actions
    if len(actions) > 1: ## only optimise if there are multiple changes to execute
        ## expand out all the groups we could use
        relevant_groups = set([])
        for entity in actions.keys():
            relevant_groups.update(l2g[entity])

        ## for each group we could use, can we use it to get to target?
        relevant_groups = list(sorted(relevant_groups, key= lambda g:len(g2l[g])))
        for g in relevant_groups:
            ls = g2l.get(g, set())
            states = set([target_states.get(l, current_states.get(l, None)) for l in ls])
            if len(states) == 1: # can use group
                # is it better to use it or not?
                # this is how many distinct actions we already have for these lights
                cur = len(set([action_entity[l] for l in ls if l in action_entity]))
                if cur > 1:
                    for l in ls:
                        action_entity[l] = g
                        actions[g] = list(states)[0]
        actions_2 = {}
        for e,a in actions.items():
            if e in action_entity:
                actions_2[action_entity[e]] = a
        actions = actions_2

    return actions
        
def curve(times, params):
    now, sunrise, noon, sunset = times
    (k, x, minimum, maximum) = params
    if now < noon:
        x = (1+tanh(k*(now - (sunrise + x))))/2
    else:
        x = (1+tanh(k*(sunset - (now + x))))/2
    return int(minimum + (maximum - minimum) * x)

def get_times(hass):
    now = dt_util.utcnow() #await self.hass.async_add_executor_job(dt_util.utcnow)
    loc, _ = get_astral_location(hass)
    today = now.replace(hour = 0, minute = 0, second = 0)
    sunrise = loc.sunrise(today)
    sunset = loc.sunset(today)
    noon = loc.noon(today)

    sunrise = (sunrise.hour + sunrise.minute / 60) / 24
    sunset = (sunset.hour + sunset.minute / 60) / 24
    noon = (noon.hour + noon.minute / 60) / 24
    now = (now.hour + now.minute / 60) / 24

    return (now, sunrise, noon, sunset)

##################### poking about in zha ##########

@pyscript_compile
def zha_expand(entities):
    g2l, l2g = zha_group_map()
    out = set()
    for e in entities:
        if len(g2l[e]): out.update(g2l[e])
        else: out.add(e)
    return out

@pyscript_compile
def zha_group_members(ref, domain):
    members = ref.entity_data.group_proxy.group_info["members"]
    return [entity["entity_id"]
            for member in members
            for entity in member.get("device",{}).get("entities",[])
            if entity["entity_id"].startswith(domain)]

# maybe I should be caching this
@pyscript_compile
def zha_group_map():
    g2l = defaultdict(set)
    l2g = defaultdict(set)
    for refs in hass.data['zha'].gateway_proxy.ha_entity_refs.values():
        for ref in refs:
            entity_id = ref.ha_entity_id
            if ref.entity_data.group_proxy:
                members = zha_group_members(ref, 'light.')
                g2l[entity_id].update(members)
                for m in members: l2g[m].add(entity_id)
    # if a group is fully within another group, l2g[sub] -> super
    for g,ls in g2l.items():
        other_groups = [g2 for l in ls
                        for g2 in l2g[l]
                        if g2l[g2] > ls]
        for g2 in other_groups:
            l2g[g].add(g2)
    return (g2l, l2g)

########### intercepting off/on/toggle ################

context = Context()

@pyscript_compile
async def intercept(call, data):
    global context
    # skip our own calls
    if call.context == context: return
    if call.service == SERVICE_TURN_ON:
        await intercept_on(data, zha_expand(data.get(ATTR_ENTITY_ID)))
    elif call.service == SERVICE_TURN_OFF:
        latch_off(zha_expand(data.get(ATTR_ENTITY_ID)))
    elif call.service == SERVICE_TOGGLE:
        entities = data.get(ATTR_ENTITY_ID)
        offs,ons = [],[]

        current_state = {}
        target_state = {}
        # this won't necessarily work quite right
        # as we might not have members of every group
        for e in entities:
            s = hass.states.get(e)
            if s:
                if s.state == STATE_ON:
                    current_state[e] = True
                    target_state[e] = False
                elif s.state == STATE_OFF:
                    current_state[e] = False
                    target_state[e] = True
        
        actions = reconcile(current_state, target_state)
        for (e, a) in actions.items():
            if a: ons.append(e)
            else: offs.append(e)
        data[ATTR_ENTITY_ID] = offs
        latch_off(offs)
        if len(ons):
            # we will maybe intercept this again and insert any needed
            # brightness parameters? not really sure this will work
            # properly anyway
            await hass.services.async_call(
                "light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ons}
            )
        
@pyscript_compile
async def intercept_on(data, expanded_entities):
    global lighsets, managed_lights, context
    # does it affect our entities?
    params = data["params"]
    latches =  ATTR_BRIGHTNESS in params \
        or ATTR_BRIGHTNESS_PCT in params \
        or ATTR_BRIGHTNESS_STEP_PCT in params \
        or ATTR_BRIGHTNESS_PCT in params \
        or ATTR_COLOR_TEMP_KELVIN in params \
        or ATTR_RGB_COLOR in params \
        or ATTR_HS_COLOR in params \
        or ATTR_RGBW_COLOR in params \
        or ATTR_RGBWW_COLOR in params \
        or ATTR_XY_COLOR in params \
        or ATTR_WHITE in params \
        or ATTR_COLOR_NAME in params
    if latches:
        # TODO could zigtimise here
        for entity in expanded_entities:
            if entity in managed_lights:
                if not(managed_lights[entity]["lock"]):
                    managed_lights[entity]["latch"] = True

        if ATTR_TRANSITION in params and \
           ATTR_BRIGHTNESS in params and \
           ATTR_COLOR_TEMP_KELVIN in params:
            # we are running within the interceptor, so whatever
            # we leave alone will happen first; that will be transition brightness
            temp = params[ATTR_COLOR_TEMP_KELVIN]
            del params[ATTR_COLOR_TEMP_KELVIN]
            params[ATTR_TRANSITION] = params[ATTR_TRANSITION] / 2
            # later we want to do the next thing
            hass.async_create_task(
                turn_on(
                    {ATTR_ENTITY_ID: data[ATTR_ENTITY_ID], ATTR_COLOR_TEMP_KELVIN: temp},
                    params[ATTR_TRANSITION]+0.1
                )
            )
    else:
        # early abort
        for entity in expanded_entities:
            if entity in managed_lights: break
        else: return

        target_state = {}
        current_state = {}
        for entity in expanded_entities:
            st = managed_lights.get(entity, {"latch":False, "lock":None, "lightset":None})
            ls = lightsets.get(st["lightset"], {"state":True}).get("state")
            if st["latch"]:
                target_state[entity] = True
            elif st["lock"]:
                target_state[entity] = st["lock"]
            else:
                target_state[entity] = ls
            cur_st = hass.states.get(entity)
            if cur_st and cur_st.state == STATE_ON:
                if target_state[entity] == True:
                    current_state[entity] = True
                else:
                    current_state[entity] = (cur_st.attributes.get(ATTR_BRIGHTNESS),
                                             cur_st.attributes.get(ATTR_COLOR_TEMP_KELVIN))
            else:
                current_state[entity] = False
        actions = reconcile(current_state, target_state)
        actions = list(actions.items())
        if actions:
            (entity, action) = actions[0]
            data[ATTR_ENTITY_ID] = [entity]
            # body own action
            if type(action) is tuple:
                params[ATTR_BRIGHTNESS] = action[0]
                params[ATTR_COLOR_TEMP_KELVIN] = action[1]
            if ATTR_TRANSITION in params and \
               ATTR_BRIGHTNESS in params and \
               ATTR_COLOR_TEMP_KELVIN in params:
                # we are running within the interceptor, so whatever
                # we leave alone will happen first; that will be transition brightness
                temp = params[ATTR_COLOR_TEMP_KELVIN]
                params[ATTR_TRANSITION] = params[ATTR_TRANSITION] / 2
                del params[ATTR_COLOR_TEMP_KELVIN]
                # later we want to do the next thing
                hass.async_create_task(
                    turn_on(
                        {ATTR_ENTITY_ID: [entity],
                         ATTR_COLOR_TEMP_KELVIN: temp,
                         ATTR_TRANSITION: params[ATTR_TRANSITION]},
                        params[ATTR_TRANSITION]+0.1
                    )
                )
                
            # extra actions:
            for (entity, action) in actions[1:]:
                call_data = params | {ATTR_ENTITY_ID: [entity]}
                if type(action) is tuple:
                    call_data = call_data | {
                        ATTR_BRIGHTNESS: action[0],
                        ATTR_COLOR_TEMP_KELVIN: action[1]
                    }
                hass.async_create_task(turn_on(call_data))
    
@pyscript_compile
async def turn_on(data, delay = 0):
    if delay:
        await asyncio.sleep(delay)
    if ATTR_TRANSITION in data and \
       ATTR_BRIGHTNESS in data and \
       ATTR_COLOR_TEMP_KELVIN in data:
        data[ATTR_TRANSITION] = data[ATTR_TRANSITION]/2
        temp = data[ATTR_COLOR_TEMP_KELVIN]
        del data[ATTR_COLOR_TEMP_KELVIN]
        await hass.services.async_call(
            "light", SERVICE_TURN_ON, data, context = context
        )
        asyncio.sleep(data[ATTR_TRANSITION]+0.1)
        del data[ATTR_BRIGHTNESS]
        data[ATTR_COLOR_TEMP_KELVIN] = temp
        await hass.services.async_call(
            "light", SERVICE_TURN_ON, data, context = context
        )
    else:
        await hass.services.async_call(
            "light", SERVICE_TURN_ON, data, context = context
        )

@pyscript_compile
def latch_off(entities):
    global managed_lights
    for e in entities:
        if managed_lights.get(e, {}).get("latch", False):
            managed_lights[e]["latch"] = False
            managed_lights[e]["lock"] = None

interceptors = []

@time_trigger('startup')
def init():
    global interceptors
    interceptors.extend([
        setup_service_call_interceptor( hass, 'light', SERVICE_TURN_ON, intercept ),
        setup_service_call_interceptor( hass, 'light', SERVICE_TURN_OFF, intercept ),
        setup_service_call_interceptor( hass, 'light', SERVICE_TOGGLE, intercept )
    ])

@time_trigger('shutdown')
def cleanup():
    global interceptors
    for i in interceptors: i()
    interceptors = []
