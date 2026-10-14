#include "search.hpp"
#include <algorithm>
#include <bit>
#include <cmath>

namespace chess {
namespace {
constexpr int values[]{0, 100, 320, 330, 500, 900, 0};
int distance(int a, int b) {
    return std::max(std::abs(a % 8 - b % 8), std::abs(a / 8 - b / 8));
}
int center(int sq) {
    return std::abs(2 * (sq % 8) - 7) + std::abs(2 * (sq / 8) - 7);
}
// Generated once: a small positional table, indexed from White's perspective.
const auto pst = [] {
    std::array<std::array<int, 64>, 7> t{};
    for (int sq = 0; sq < 64; ++sq) {
        int r = sq / 8, c = center(sq);
        t[PAWN][sq] = r * 7 - c * 2;
        t[KNIGHT][sq] = 36 - c * 5;
        t[BISHOP][sq] = 24 - c * 3;
        t[ROOK][sq] = r * 2 + (r == 6 ? 15 : 0);
        t[QUEEN][sq] = 14 - c;
        t[KING][sq] = -r * 8 + c * 2;
    }
    return t;
}();
int stored_score(int score, int ply) {
    return score > MATE - 128 ? score + ply : score < -MATE + 128 ? score - ply : score;
}
int loaded_score(int score, int ply) {
    return score > MATE - 128 ? score - ply : score < -MATE + 128 ? score + ply : score;
}
} // namespace
int evaluate(const Board &board) {
    const auto &s = board.state;
    int score = 0, nonpawn = 0;
    for (int c = 0; c < 2; ++c)
        for (int p = KNIGHT; p <= QUEEN; ++p)
            nonpawn += std::popcount(s.pieces[c][p]) * values[p];
    for (int c = 0; c < 2; ++c) {
        int subtotal = 0, king = std::countr_zero(s.pieces[c][KING]);
        for (int p = PAWN; p <= KING; ++p) {
            Bits b = s.pieces[c][p];
            while (b) {
                int sq = std::countr_zero(b);
                b &= b - 1;
                int oriented = c == 0 ? sq : sq ^ 56;
                subtotal += values[p] +
                            (p == KING && nonpawn < 1400 ? 40 - center(sq) * 4 : pst[p][oriented]);
                if (p == PAWN) {
                    bool passed = true;
                    Bits enemy = s.pieces[1 - c][PAWN];
                    while (enemy) {
                        int other = std::countr_zero(enemy);
                        enemy &= enemy - 1;
                        if (std::abs(other % 8 - sq % 8) <= 1 &&
                            (c == 0 ? other / 8 > sq / 8 : other / 8 < sq / 8)) {
                            passed = false;
                            break;
                        }
                    }
                    int advance = oriented / 8;
                    if (passed)
                        subtotal += advance * advance * 4;
                    if (nonpawn < 1400)
                        subtotal += 14 - distance(king, sq) * 3;
                }
            }
        }
        // Guide elementary queen/rook mates: drive the bare king to an edge
        // while bringing the attacking king closer, without overriding search.
        Bits enemy_nonking = 0, own_other = 0;
        for (int p = PAWN; p <= QUEEN; ++p)
            enemy_nonking |= s.pieces[1 - c][p];
        for (int p = PAWN; p <= BISHOP; ++p)
            own_other |= s.pieces[c][p];
        if (!enemy_nonking && !own_other && (s.pieces[c][ROOK] || s.pieces[c][QUEEN])) {
            int enemy_king = std::countr_zero(s.pieces[1 - c][KING]);
            int king_distance =
                std::abs(king % 8 - enemy_king % 8) + std::abs(king / 8 - enemy_king / 8);
            subtotal += center(enemy_king) * 12 + (14 - king_distance) * 18;
            // Reward shrinking the rectangle cut off by the major piece.
            // Edge/king-distance terms alone allow rook shuffling without
            // actually reducing the defender's space.
            Bits majors = s.pieces[c][ROOK] | s.pieces[c][QUEEN];
            int smallest = 64;
            while (majors) {
                int sq = std::countr_zero(majors);
                majors &= majors - 1;
                int f = sq % 8, r = sq / 8, ef = enemy_king % 8, er = enemy_king / 8;
                int width = ef > f ? 7 - f : ef < f ? f : 8;
                int height = er > r ? 7 - r : er < r ? r : 8;
                smallest = std::min(smallest, width * height);
            }
            subtotal += (64 - smallest) * 6;
        }
        score += c == 0 ? subtotal : -subtotal;
    }
    return std::clamp(s.side == 0 ? score : -score, -MATE + 256, MATE - 256);
}
Search::Search(std::size_t megabytes) {
    resize(megabytes);
}
void Search::resize(std::size_t megabytes) {
    table.assign(std::max<std::size_t>(1, megabytes * 1024 * 1024 / sizeof(Entry)), {});
}
void Search::clear() {
    std::fill(table.begin(), table.end(), Entry{});
}
bool Search::stopped() {
    if (stop_flag->load(std::memory_order_relaxed) ||
        (timed && std::chrono::steady_clock::now() >= deadline))
        interrupted = true;
    return interrupted;
}
Bits Search::search_key(const Board &b) const {
    // Include reversible history and the fifty-move counter. Position-only TT
    // scores can otherwise turn a history-dependent draw into a false win.
    Bits h = b.key() ^ (static_cast<Bits>(b.state.halfmove) * 0x9e3779b97f4a7c15ULL);
    std::size_t first =
        b.history.size() - 1 - std::min<std::size_t>(b.state.halfmove, b.history.size() - 1);
    for (std::size_t i = first; i < b.history.size(); ++i)
        h = std::rotl(h, 7) ^ b.history[i];
    return h;
}
void Search::order(const Board &b, MoveList &moves, Move preferred) {
    auto rank = [&](Move m) {
        if (m == preferred)
            return 100000;
        int victim = std::abs(b.state.squares[m.to()]);
        if (b.is_capture(m) && !victim)
            victim = PAWN;
        return (victim ? 10000 + values[victim] * 10 - values[std::abs(b.state.squares[m.from()])]
                       : 0) +
               values[m.promotion()];
    };
    std::sort(moves.begin(), moves.end(), [&](Move a, Move other) {
        int x = rank(a), y = rank(other);
        return x == y ? a.value < other.value : x > y;
    });
}
int Search::quiescence(Board &b, int alpha, int beta, int ply, int quiet_checks) {
    ++nodes;
    if ((nodes & 255) == 0 && stopped())
        return 0;
    auto moves = b.legal_moves();
    bool check = b.in_check();
    if (!moves.size)
        return check ? -MATE + ply : 0;
    if (b.draw())
        return 0;
    if (ply >= 96)
        return evaluate(b);
    if (!check) {
        int stand = evaluate(b);
        if (stand >= beta)
            return stand;
        alpha = std::max(alpha, stand);
    }
    order(b, moves, {});
    Bits nonkings = 0;
    for (int c = 0; c < 2; ++c)
        for (int p = PAWN; p <= QUEEN; ++p)
            nonkings |= b.state.pieces[c][p];
    bool elementary =
        std::popcount(nonkings) == 1 && (b.state.pieces[0][ROOK] | b.state.pieces[1][ROOK] |
                                         b.state.pieces[0][QUEEN] | b.state.pieces[1][QUEEN]);
    for (Move m : moves) {
        bool quiet = !check && !b.is_capture(m) && !m.promotion();
        if (quiet && (!elementary || quiet_checks == 0))
            continue;
        State old = b.make(m);
        if (quiet && !b.in_check()) {
            b.unmake(old);
            continue;
        }
        int score = -quiescence(b, -beta, -alpha, ply + 1, quiet_checks - (quiet ? 1 : 0));
        b.unmake(old);
        if (interrupted)
            return 0;
        if (score >= beta)
            return score;
        alpha = std::max(alpha, score);
    }
    return alpha;
}
int Search::negamax(Board &b, int depth, int alpha, int beta, int ply) {
    if (depth <= 0)
        return quiescence(b, alpha, beta, ply);
    ++nodes;
    if ((nodes & 255) == 0 && stopped())
        return 0;
    auto moves = b.legal_moves();
    if (!moves.size)
        return b.in_check() ? -MATE + ply : 0;
    if (b.draw())
        return 0;
    if (ply >= 96)
        return evaluate(b);
    Bits key = search_key(b);
    Entry &entry = table[key % table.size()];
    Move preferred;
    if (entry.key == key && entry.depth >= 0) {
        preferred = entry.move;
        if (ply > 0 && entry.depth >= depth) {
            int score = loaded_score(entry.score, ply);
            if (entry.bound == 0 || (entry.bound == 1 && score >= beta) ||
                (entry.bound == 2 && score <= alpha))
                return score;
        }
    }
    order(b, moves, preferred);
    int original_alpha = alpha, best = -MATE;
    Move best_move = moves.moves[0];
    for (Move m : moves) {
        State old = b.make(m);
        int score = -negamax(b, depth - 1, -beta, -alpha, ply + 1);
        b.unmake(old);
        if (interrupted)
            return 0;
        if (score > best) {
            best = score;
            best_move = m;
            if (ply == 0)
                root_best = m;
        }
        alpha = std::max(alpha, score);
        if (alpha >= beta)
            break;
    }
    entry = {key, best_move, static_cast<std::int16_t>(stored_score(best, ply)),
             static_cast<std::int8_t>(depth),
             static_cast<std::uint8_t>(best >= beta             ? 1
                                       : best <= original_alpha ? 2
                                                                : 0)};
    return best;
}
std::vector<Move> Search::principal_variation(Board b, int depth) {
    std::vector<Move> result;
    for (int i = 0; i < depth; ++i) {
        Bits key = search_key(b);
        const auto &entry = table[key % table.size()];
        if (entry.key != key || !entry.move)
            break;
        auto moves = b.legal_moves();
        if (std::find(moves.begin(), moves.end(), entry.move) == moves.end())
            break;
        result.push_back(entry.move);
        b.make(entry.move);
        if (b.draw())
            break;
    }
    return result;
}
Move Search::run(Board b, Limits limits, std::atomic_bool &stop,
                 const std::function<void(const SearchInfo &)> &report) {
    b.history.reserve(b.history.size() + 128);
    start = std::chrono::steady_clock::now();
    timed = limits.milliseconds >= 0;
    deadline = start + std::chrono::milliseconds(std::max(0, limits.milliseconds));
    stop_flag = &stop;
    nodes = 0;
    interrupted = false;
    auto moves = b.legal_moves();
    if (!moves.size)
        return {};
    Move best = moves.moves[0];
    for (int depth = 1; depth <= std::clamp(limits.depth, 1, 64); ++depth) {
        if (stopped())
            break;
        root_best = best;
        int score = negamax(b, depth, -MATE, MATE, 0);
        if (interrupted)
            break;
        best = root_best;
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                           std::chrono::steady_clock::now() - start)
                           .count();
        report({depth, score, nodes, elapsed, principal_variation(b, depth)});
        if (std::abs(score) >= MATE - 96 || b.draw())
            break;
    }
    return best;
}
} // namespace chess
