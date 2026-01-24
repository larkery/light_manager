from .interceptor import setup_service_call_interceptor

import homeassistant.util.dt as dt_util
from homeassistant.helpers.sun import get_astral_location
from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN
)

@time_trigger('startup')
def init():
    pass

@time_trigger('shutdown'):
def cleanup():
    pass

# block name -> settings
lightsets = {}

# light entity -> stuff
managed_lights = {}

def manage(lightset,
           lights, # a list of entity IDs
           brightness_min,
           brightness_max,
           brightness_k,
           brightness_x,
           temperature_min,
           temperature_max,
           temperature_k,
           temperature_x):
    global lightsets, managed_lights
    lights = zha_expand(lights)
    unmanage(lights)
    
    # re-manage them
    for light in lights:
        managed_lights[light] = {
            "lightset": lightset, 
            "lock" = None, # or (brightness, temperature)
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
    for light in lights + [k for (k,v) in managed_lights.items()
                           if v["lightset"] == lightset]:
        del managed_lights[light]

def update:
    global lightsets, managed_lights

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
    
    target_states = {name:(managed_lights[name]["lock"] or
                           set_states[managed_lights[name]["lightset"]])
                     for name in managed_lights
                     if current_states[name][0]}

    log.warning(f"AIM FOR {target_state}")
        
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

@pyscript_compile
def zha_expand(entities):
    l2g, g2l = zha_group_map()
    out = set()
    for e in entities:
        if e in g2l: out.update(g2l[e])
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
                members = group_members(ref, 'light.')
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
