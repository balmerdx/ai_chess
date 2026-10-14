#include "board.hpp"
#include <algorithm>
#include <bit>
#include <cctype>
#include <cmath>
#include <limits>
#include <sstream>

namespace chess {
namespace {
int color(int p) {
    return p < 0 ? 1 : 0;
}
Bits bit(int sq) {
    return Bits{1} << sq;
}
constexpr std::array<std::array<int, 2>, 8> knight_steps{
    {{1, 2}, {2, 1}, {2, -1}, {1, -2}, {-1, -2}, {-2, -1}, {-2, 1}, {-1, 2}}};
constexpr std::array<std::array<int, 2>, 8> directions{
    {{1, 0}, {-1, 0}, {0, 1}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}}};
struct AttackTables {
    std::array<Bits, 64> knights{}, kings{};
    std::array<std::array<Bits, 64>, 2> pawns{};
    AttackTables() {
        for (int sq = 0; sq < 64; ++sq) {
            int f = sq % 8, r = sq / 8;
            for (auto d : knight_steps) {
                int x = f + d[0], y = r + d[1];
                if (x >= 0 && x < 8 && y >= 0 && y < 8)
                    knights[sq] |= bit(y * 8 + x);
            }
            for (auto d : directions) {
                int x = f + d[0], y = r + d[1];
                if (x >= 0 && x < 8 && y >= 0 && y < 8)
                    kings[sq] |= bit(y * 8 + x);
            }
            for (int c = 0; c < 2; ++c)
                for (int df : {-1, 1}) {
                    int x = f + df, y = r + (c == 0 ? 1 : -1);
                    if (x >= 0 && x < 8 && y >= 0 && y < 8)
                        pawns[c][sq] |= bit(y * 8 + x);
                }
        }
    }
};
const AttackTables attacks;
bool attacked_state(const State &s, int sq, int by) {
    int sign = by == 0 ? 1 : -1;
    Bits targets = attacks.pawns[1 - by][sq];
    while (targets) {
        int t = std::countr_zero(targets);
        targets &= targets - 1;
        if (s.squares[t] == sign * PAWN)
            return true;
    }
    targets = attacks.knights[sq];
    while (targets) {
        int t = std::countr_zero(targets);
        targets &= targets - 1;
        if (s.squares[t] == sign * KNIGHT)
            return true;
    }
    targets = attacks.kings[sq];
    while (targets) {
        int t = std::countr_zero(targets);
        targets &= targets - 1;
        if (s.squares[t] == sign * KING)
            return true;
    }
    for (int i = 0; i < 8; ++i) {
        auto d = directions[i];
        int f = sq % 8 + d[0], r = sq / 8 + d[1];
        while (f >= 0 && f < 8 && r >= 0 && r < 8) {
            int p = s.squares[r * 8 + f];
            if (p) {
                if (color(p) == by &&
                    (std::abs(p) == QUEEN || std::abs(p) == (i < 4 ? ROOK : BISHOP)))
                    return true;
                break;
            }
            f += d[0];
            r += d[1];
        }
    }
    return false;
}
struct Zobrist {
    std::array<std::array<Bits, 64>, 12> pieces{};
    std::array<Bits, 16> rights{};
    std::array<Bits, 8> ep{};
    Bits side;
    Zobrist() {
        Bits seed = 0x1378ac05e8219f42ULL;
        auto next = [&]() {
            seed += 0x9e3779b97f4a7c15ULL;
            Bits z = seed;
            z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
            z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
            return z ^ (z >> 31);
        };
        for (auto &p : pieces)
            for (auto &x : p)
                x = next();
        for (auto &x : rights)
            x = next();
        for (auto &x : ep)
            x = next();
        side = next();
    }
};
const Zobrist zobrist;
std::string square_name(int sq) {
    return std::string{static_cast<char>('a' + sq % 8), static_cast<char>('1' + sq / 8)};
}
} // namespace
std::string Move::uci() const {
    if (!*this)
        return "0000";
    std::string text = square_name(from()) + square_name(to());
    if (promotion())
        text += "  nbrqk"[promotion()];
    return text;
}
Board::Board() {
    history.reserve(2048);
    set_fen(START_FEN);
}
void Board::put(int sq, int p) {
    int old = state.squares[sq];
    if (old)
        state.pieces[color(old)][std::abs(old)] &= ~bit(sq);
    state.squares[sq] = static_cast<std::int8_t>(p);
    if (p)
        state.pieces[color(p)][std::abs(p)] |= bit(sq);
}
bool Board::set_fen(std::string_view fen_text, std::string *error) {
    auto fail = [&](const char *why) {
        if (error)
            *error = why;
        return false;
    };
    std::istringstream in{std::string(fen_text)};
    std::string layout, side, rights, ep, extra;
    State candidate;
    if (!(in >> layout >> side >> rights >> ep >> candidate.halfmove >> candidate.fullmove) ||
        (in >> extra))
        return fail("FEN requires six fields");
    if (candidate.halfmove < 0 || candidate.fullmove < 1)
        return fail("Invalid move counters");
    int rank = 7, file = 0;
    for (char ch : layout) {
        if (ch == '/') {
            if (file != 8 || rank == 0)
                return fail("Invalid FEN ranks");
            --rank;
            file = 0;
            continue;
        }
        if (ch >= '1' && ch <= '8') {
            file += ch - '0';
            if (file > 8)
                return fail("Invalid rank width");
            continue;
        }
        auto found = std::string_view("pnbrqk").find(
            static_cast<char>(std::tolower(static_cast<unsigned char>(ch))));
        if (found == std::string_view::npos || file >= 8)
            return fail("Invalid FEN piece");
        int p = static_cast<int>(found) + 1,
            c = std::isupper(static_cast<unsigned char>(ch)) ? 0 : 1, sq = rank * 8 + file++;
        if (p == PAWN && (rank == 0 || rank == 7))
            return fail("Pawn on promotion rank");
        candidate.squares[sq] = static_cast<std::int8_t>(c == 0 ? p : -p);
        candidate.pieces[c][p] |= bit(sq);
    }
    if (rank != 0 || file != 8 || (side != "w" && side != "b"))
        return fail("Invalid board or side");
    candidate.side = side == "w" ? 0 : 1;
    if (rights != "-")
        for (char ch : rights) {
            auto i = std::string_view("KQkq").find(ch);
            if (i == std::string_view::npos || (candidate.rights & (1 << i)))
                return fail("Invalid castling rights");
            candidate.rights |= 1 << i;
        }
    if (ep != "-") {
        if (ep.size() != 2 || ep[0] < 'a' || ep[0] > 'h' ||
            ep[1] != (candidate.side == 0 ? '6' : '3'))
            return fail("Invalid en passant square");
        candidate.ep = (ep[1] - '1') * 8 + ep[0] - 'a';
        if (candidate.squares[candidate.ep] ||
            candidate.squares[candidate.ep + (candidate.side == 0 ? -8 : 8)] !=
                (candidate.side == 0 ? -PAWN : PAWN) ||
            candidate.squares[candidate.ep + (candidate.side == 0 ? 8 : -8)])
            return fail("Invalid en passant pawn");
    }
    for (int c = 0; c < 2; ++c) {
        if (std::popcount(candidate.pieces[c][KING]) != 1)
            return fail("Exactly one king per side required");
        Bits occupied = 0;
        for (int p = PAWN; p <= KING; ++p)
            occupied |= candidate.pieces[c][p];
        if (std::popcount(occupied) > 16 || std::popcount(candidate.pieces[c][PAWN]) > 8)
            return fail("Too many pieces");
    }
    const std::array<int, 4> rook_sq{7, 0, 63, 56};
    for (int i = 0; i < 4; ++i)
        if (candidate.rights & (1 << i)) {
            int sign = i < 2 ? 1 : -1, king_sq = i < 2 ? 4 : 60;
            if (candidate.squares[rook_sq[i]] != sign * ROOK ||
                candidate.squares[king_sq] != sign * KING)
                return fail("Castling pieces missing");
        }
    int previous_king = std::countr_zero(candidate.pieces[1 - candidate.side][KING]);
    if (attacked_state(candidate, previous_king, candidate.side))
        return fail("Side not to move is in check");
    state = candidate;
    history.clear();
    history.push_back(key());
    return true;
}
std::string Board::fen() const {
    std::ostringstream out;
    for (int r = 7; r >= 0; --r) {
        int empty = 0;
        for (int f = 0; f < 8; ++f) {
            int p = state.squares[r * 8 + f];
            if (!p) {
                ++empty;
                continue;
            }
            if (empty) {
                out << empty;
                empty = 0;
            }
            char ch = " pnbrqk"[std::abs(p)];
            out << static_cast<char>(p > 0 ? std::toupper(ch) : ch);
        }
        if (empty)
            out << empty;
        if (r)
            out << '/';
    }
    out << (state.side == 0 ? " w " : " b ");
    if (!state.rights)
        out << '-';
    else
        for (int i = 0; i < 4; ++i)
            if (state.rights & (1 << i))
                out << "KQkq"[i];
    out << ' ' << (state.ep < 0 ? "-" : square_name(state.ep)) << ' ' << state.halfmove << ' '
        << state.fullmove;
    return out.str();
}
Bits Board::key() const {
    Bits result = zobrist.rights[state.rights] ^ (state.side ? zobrist.side : 0);
    for (int c = 0; c < 2; ++c)
        for (int p = PAWN; p <= KING; ++p) {
            Bits b = state.pieces[c][p];
            while (b) {
                int sq = std::countr_zero(b);
                b &= b - 1;
                result ^= zobrist.pieces[c * 6 + p - 1][sq];
            }
        }
    // A pinned en-passant pawn must not change repetition identity.
    if (state.ep >= 0) {
        Bits candidates = attacks.pawns[1 - state.side][state.ep] & state.pieces[state.side][PAWN];
        while (candidates) {
            int sq = std::countr_zero(candidates);
            candidates &= candidates - 1;
            State temp = state;
            temp.squares[state.ep] = temp.squares[sq];
            temp.squares[sq] = 0;
            temp.squares[state.ep + (state.side == 0 ? -8 : 8)] = 0;
            if (!attacked_state(temp, std::countr_zero(state.pieces[state.side][KING]),
                                1 - state.side)) {
                result ^= zobrist.ep[state.ep % 8];
                break;
            }
        }
    }
    return result;
}
bool Board::attacked(int square, int by) const {
    return attacked_state(state, square, by);
}
bool Board::in_check() const {
    return attacked(std::countr_zero(state.pieces[state.side][KING]), 1 - state.side);
}
MoveList Board::pseudo_moves() const {
    MoveList list;
    int side = state.side, sign = side == 0 ? 1 : -1;
    auto add = [&](int from, int to) {
        if (std::abs(state.squares[from]) == PAWN && (to / 8 == 0 || to / 8 == 7))
            for (int p : {QUEEN, ROOK, BISHOP, KNIGHT})
                list.add(Move(from, to, p));
        else
            list.add(Move(from, to));
    };
    for (int p = PAWN; p <= KING; ++p) {
        Bits pieces = state.pieces[side][p];
        while (pieces) {
            int from = std::countr_zero(pieces);
            pieces &= pieces - 1;
            int f = from % 8, r = from / 8;
            if (p == PAWN) {
                int to = from + sign * 8;
                if (to >= 0 && to < 64 && !state.squares[to]) {
                    add(from, to);
                    int twice = from + sign * 16;
                    if (r == (side == 0 ? 1 : 6) && !state.squares[twice])
                        add(from, twice);
                }
                Bits targets = attacks.pawns[side][from];
                while (targets) {
                    int t = std::countr_zero(targets);
                    targets &= targets - 1;
                    int victim = state.squares[t];
                    if ((victim && color(victim) != side && std::abs(victim) != KING) ||
                        t == state.ep)
                        add(from, t);
                }
            } else if (p == KNIGHT || p == KING) {
                Bits targets = p == KNIGHT ? attacks.knights[from] : attacks.kings[from];
                while (targets) {
                    int t = std::countr_zero(targets);
                    targets &= targets - 1;
                    int victim = state.squares[t];
                    if (!victim || (color(victim) != side && std::abs(victim) != KING))
                        add(from, t);
                }
                if (p == KING) {
                    int home = side == 0 ? 4 : 60, shift = side * 2;
                    if (from == home && !attacked(home, 1 - side)) {
                        if ((state.rights & (1 << shift)) && !state.squares[home + 1] &&
                            !state.squares[home + 2] && !attacked(home + 1, 1 - side) &&
                            !attacked(home + 2, 1 - side))
                            add(home, home + 2);
                        if ((state.rights & (2 << shift)) && !state.squares[home - 1] &&
                            !state.squares[home - 2] && !state.squares[home - 3] &&
                            !attacked(home - 1, 1 - side) && !attacked(home - 2, 1 - side))
                            add(home, home - 2);
                    }
                }
            } else {
                int first = p == BISHOP ? 4 : 0, last = p == ROOK ? 4 : 8;
                for (int i = first; i < last; ++i) {
                    auto d = directions[i];
                    int x = f + d[0], y = r + d[1];
                    while (x >= 0 && x < 8 && y >= 0 && y < 8) {
                        int t = y * 8 + x, victim = state.squares[t];
                        if (!victim)
                            add(from, t);
                        else {
                            if (color(victim) != side && std::abs(victim) != KING)
                                add(from, t);
                            break;
                        }
                        x += d[0];
                        y += d[1];
                    }
                }
            }
        }
    }
    return list;
}
State Board::make(Move m) {
    State old = state;
    int from = m.from(), to = m.to(), p = state.squares[from], victim = state.squares[to],
        side = state.side;
    state.halfmove =
        (std::abs(p) == PAWN || victim)
            ? 0
            : state.halfmove + (state.halfmove < std::numeric_limits<int>::max() ? 1 : 0);
    if (std::abs(p) == PAWN && to == state.ep && !victim)
        put(to + (side == 0 ? -8 : 8), 0);
    put(from, 0);
    put(to, m.promotion() ? (side == 0 ? m.promotion() : -m.promotion()) : p);
    if (std::abs(p) == KING) {
        state.rights &= side == 0 ? 12 : 3;
        if (std::abs(to - from) == 2) {
            int rook_from = to > from ? from + 3 : from - 4,
                rook_to = to > from ? from + 1 : from - 1;
            put(rook_from, 0);
            put(rook_to, side == 0 ? ROOK : -ROOK);
        }
    }
    for (int sq : {from, to}) {
        if (sq == 7)
            state.rights &= ~1;
        if (sq == 0)
            state.rights &= ~2;
        if (sq == 63)
            state.rights &= ~4;
        if (sq == 56)
            state.rights &= ~8;
    }
    state.ep = std::abs(p) == PAWN && std::abs(to - from) == 16 ? (from + to) / 2 : -1;
    state.fullmove += side && state.fullmove < std::numeric_limits<int>::max();
    state.side = 1 - side;
    history.push_back(key());
    return old;
}
void Board::unmake(const State &previous) {
    state = previous;
    history.pop_back();
}
MoveList Board::legal_moves() {
    MoveList result;
    for (Move m : pseudo_moves()) {
        State old = make(m);
        int king = std::countr_zero(state.pieces[old.side][KING]);
        bool legal = !attacked(king, state.side);
        unmake(old);
        if (legal)
            result.add(m);
    }
    return result;
}
Move Board::parse_move(std::string_view text) {
    for (Move m : legal_moves())
        if (m.uci() == text)
            return m;
    return {};
}
bool Board::is_capture(Move m) const {
    return state.squares[m.to()] ||
           (std::abs(state.squares[m.from()]) == PAWN && m.to() == state.ep);
}
bool Board::repeated(int count) const {
    int occurrences = 0;
    Bits current = history.back();
    int earliest = std::max(0, static_cast<int>(history.size()) - 1 - state.halfmove);
    for (int i = static_cast<int>(history.size()) - 1; i >= earliest; i -= 2)
        if (history[i] == current && ++occurrences >= count)
            return true;
    return false;
}
bool Board::insufficient_material() const {
    if (state.pieces[0][PAWN] || state.pieces[1][PAWN] || state.pieces[0][ROOK] ||
        state.pieces[1][ROOK] || state.pieces[0][QUEEN] || state.pieces[1][QUEEN])
        return false;
    Bits knights = state.pieces[0][KNIGHT] | state.pieces[1][KNIGHT],
         bishops = state.pieces[0][BISHOP] | state.pieces[1][BISHOP];
    if (std::popcount(knights | bishops) <= 1)
        return true;
    if (knights)
        return false;
    bool light = false, dark = false;
    while (bishops) {
        int sq = std::countr_zero(bishops);
        bishops &= bishops - 1;
        if ((sq / 8 + sq % 8) % 2)
            dark = true;
        else
            light = true;
    }
    return !(light && dark);
}
bool Board::draw() const {
    return state.halfmove >= 100 || repeated() || insufficient_material();
}
std::uint64_t perft(Board &board, int depth) {
    if (depth == 0)
        return 1;
    auto moves = board.legal_moves();
    if (depth == 1)
        return moves.size;
    std::uint64_t nodes = 0;
    for (Move m : moves) {
        State old = board.make(m);
        nodes += perft(board, depth - 1);
        board.unmake(old);
    }
    return nodes;
}
} // namespace chess
