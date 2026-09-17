fun backoffMillis(attempt: Int): Int {
    return 100 * attempt
}

fun totalWait(attempts: Int): Int {
    var total = 0
    for (i in 1..attempts) total += backoffMillis(i)
    return total
}
