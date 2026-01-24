from .interceptor import setup_service_call_interceptor

import homeassistant.util.dt as dt_util
from homeassistant.helpers.sun import get_astral_location
from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN
)

from collections import defaultdict
from math import tanh

from homeassistant.core import Context

# block name -> settings
lightsets = {}

# light entity -> stuff
managed_lights = {}

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
    lights:
      required: true
    brightness_min:
    brightness_max:
    brightness_k:
    brightness_x:
    temperature_min:
    temperature_max:
    temperature_k:
    temperature_x:
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
                       temperature_min, temperature_max)
    }

def unmanage(lights=[], lightset=None):
    global lightsets, managed_lights
    lights = zha_expand(lights)
    for light in list(lights) + [k for (k,v) in managed_lights.items()
                                 if v["lightset"] == lightset]:
        managed_lights.pop(light, None)

@time_trigger("cron(*/5 * * * *)")
def update():
    global lightsets, managed_lights, context

    times = get_times(hass)
    set_states = {name:(curve(times, parameters["brightness"]),
                        curve(times, parameters["temperature"]))
                  for (name, parameters) in lightsets.items()}
    
    current_states = {}
    for id in state.names(domain = 'light'):
        val = state.get(id)
        if val == 'unavailable': continue
        att = state.getattr(id)
        current_states[id] = (att.get(ATTR_BRIGHTNESS, None),
                              att.get(ATTR_COLOR_TEMP_KELVIN, None))

    # brightness and temperature should either be whatever
    # is in lock, or whatever the lightset says, or OFF
    # if the light is off. Next we want to reconcile these
    # and then issue the minimal set of zigbee commands
    target_states = {name:(managed_lights[name]["lock"] or
                           set_states[managed_lights[name]["lightset"]])
                     for name in managed_lights
                     if current_states[name][0]}

    actions = reconcile(current_states, target_states)

    log.warning(f"AIM FOR {target_states} execute {actions}")

    for entity, (brightness, temperature) in actions:
        # can I use context here??
        if brightness:
            hass.services.async_call(
                "light", "turn_on",
                {ATTR_BRIGHTNESS: brightness,
                 ATTR_COLOR_TEMP_KELVIN: temperature},
                context=context
            )
        else:
            hass.services.async_call(
                "light", "turn_off",
                context=context
            )


@pyscript_compile
def reconcile(current_states, target_states):
    (g2l, l2g) = zha_group_map()
    actions = {}
    action_entity = {}

    for (entity, tgt) in sorted(target_states.items(),
                                key = lambda x : -len(g2l.get(x[0], []))):
        for entity in g2l.get(entity, [entity]):
            if current_states[entity] != tgt:
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
            states = set([target_states.get(l, current_states.get(l)) for l in ls])
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
def intercept(call, data):
    global context
    # skip our own calls
    if call.context == context: return
    if call.service == 'turn_on':
        intercept_on(data)
    elif call.service == 'turn_off':
        intercept_off(data)
    elif call.service == 'toggle':
        # toggle is annoying
        pass

@pyscript_compile
def intercept_on(data):
    pass

@pyscript_compile
def intercept_off(data):
    pass

interceptors = []

@time_trigger('startup')
def init():
    global interceptors
    interceptors.append(
        setup_service_call_interceptor( hass, 'light', 'turn_on', intercept ),
        setup_service_call_interceptor( hass, 'light', 'turn_off', intercept )
        setup_service_call_interceptor( hass, 'light', 'toggle', intercept )
    )

@time_trigger('shutdown')
def cleanup():
    global interceptors
    for i in interceptors: i()
    interceptors = []
