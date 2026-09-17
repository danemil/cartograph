int byte_sum(const char *data, int len) {
    int total = 0;
    for (int i = 0; i < len; i++) total += data[i];
    return total;
}

int checksum(const char *data, int len) {
    return byte_sum(data, len) % 256;
}
