/* Linux process stays alive after the initial pthread exits, as Wine can do. */
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

static void *worker(void *unused)
{
    (void)unused;
    puts("ready");
    fflush(stdout);
    char byte;
    while (read(STDIN_FILENO, &byte, 1) > 0) {}
    return NULL;
}

int main(void)
{
    pthread_t thread;
    if (pthread_create(&thread, NULL, worker, NULL)) return 1;
    pthread_exit(NULL);
}
