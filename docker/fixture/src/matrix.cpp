int cell(int row, int col, int width) {
    return row * width + col;
}

int trace(const int *values, int width) {
    int total = 0;
    for (int i = 0; i < width; i++) total += values[cell(i, i, width)];
    return total;
}
