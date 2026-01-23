from .interceptor import setup_service_call_interceptor

call_count = 0

@pyscript_compile
def intercept(call, data):
    global call_count
    call_count = call_count + 1
    return
    
@time_trigger
def init():
    global interceptor
    if interceptor:
        log.warning('remove interceptor')
        interceptor()
    interceptor = setup_service_call_interceptor(
        hass, 'light', 'turn_on',
        intercept
    )


@time_trigger("cron(* * * * *)")
def message():
    global call_count

    log.warning(f"{call_count} calls so far")
