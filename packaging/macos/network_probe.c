/* Native path cost only. Never emit network names, addresses or interface IDs.
 * API: https://developer.apple.com/documentation/network/nw_path_t
 */
#include <Network/Network.h>
#include <dispatch/dispatch.h>
#include <stdio.h>

int main(void) {
    dispatch_semaphore_t ready = dispatch_semaphore_create(0);
    dispatch_queue_t queue = dispatch_queue_create("org.comicarchiver.network", NULL);
    nw_path_monitor_t monitor = nw_path_monitor_create();
    __block const char *kind = "unknown";
    __block const char *metered = "null";
    nw_path_monitor_set_update_handler(monitor, ^(nw_path_t path) {
        kind = "unknown";
        metered = "null";
        if (nw_path_get_status(path) == nw_path_status_satisfied) {
            if (nw_path_uses_interface_type(path, nw_interface_type_cellular)) kind = "cellular";
            else if (nw_path_uses_interface_type(path, nw_interface_type_other)) kind = "unknown";
            else if (nw_path_uses_interface_type(path, nw_interface_type_wired)) kind = "wired";
            else if (nw_path_uses_interface_type(path, nw_interface_type_wifi)) kind = "wifi";
            metered = (nw_path_is_expensive(path) || nw_path_is_constrained(path)) ? "true" : "false";
        }
        dispatch_semaphore_signal(ready);
    });
    nw_path_monitor_set_queue(monitor, queue);
    nw_path_monitor_start(monitor);
    long timed_out = dispatch_semaphore_wait(ready, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC));
    /* Stop updates and drain the queue before reading the callback's final result. */
    nw_path_monitor_cancel(monitor);
    dispatch_sync(queue, ^{});
    if (timed_out) { kind = "unknown"; metered = "null"; }
    printf("{\"connection\":\"%s\",\"metered\":%s}\n", kind, metered);
    return 0;
}
